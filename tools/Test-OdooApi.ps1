<#
.SYNOPSIS
    Prüft ein (Produktiv-)Odoo-System darauf, welche externen APIs verfügbar und nutzbar sind.

.DESCRIPTION
    Das Skript führt ausschließlich LESENDE Aufrufe aus (version, login, search_count,
    fields_get, Zugriffsrechte prüfen). Es legt keine Datensätze an und ändert nichts.

    Geprüft werden:
      * Erreichbarkeit / Version            (/web/login, /web/webclient/version_info)
      * XML-RPC                             (/xmlrpc/2/common, /xmlrpc/2/object)
      * JSON-RPC                            (/jsonrpc)
      * JSON-2 API (ab Odoo 19)             (/json/2/<model>/<method>, Bearer API-Key)
      * Datenbank-Liste                     (/web/database/list)
      * Optionale REST-Pfade von Drittmodulen (-ExtraPaths)
    Mit Zugangsdaten zusätzlich:
      * Login per XML-RPC / JSON-RPC / JSON-2
      * Benutzerrechte (Admin, Vertrieb), installierte Module (crm, mail, contacts ...)
      * Zugriffsrechte auf crm.lead, res.partner, mail.message, ir.attachment, crm.tag
      * Mengengerüst (Anzahl Kontakte, Leads, Nachrichten) und Felder, die der Importer nutzt
      * Latenz pro API-Aufruf (wichtig für die Abschätzung des Massenimports)

    Läuft unter Windows PowerShell 5.1 und PowerShell 7+ (Windows/Linux/macOS).

.PARAMETER Url
    Basis-URL des Odoo-Systems, z.B. https://erp.firma.de

.PARAMETER Database
    Name der Odoo-Datenbank. Wird weggelassen, versucht das Skript sie über die
    Datenbankliste zu ermitteln (funktioniert nur, wenn list_db aktiv ist).

.PARAMETER User
    Login des Odoo-Benutzers (meist die E-Mail-Adresse).

.PARAMETER ApiKey
    API-Schlüssel (empfohlen) oder Passwort als SecureString. Fehlt er, wird die
    Umgebungsvariable ODOO_API_KEY benutzt oder interaktiv nachgefragt.
    API-Schlüssel anlegen: Odoo -> Mein Profil -> Kontosicherheit -> "Neuer API-Schlüssel".

.PARAMETER NoAuth
    Nur anonyme Prüfungen ausführen (keine Anmeldung).

.PARAMETER SkipCertificateCheck
    TLS-Zertifikat nicht prüfen (nur für Testsysteme mit selbstsigniertem Zertifikat!).

.PARAMETER OutFile
    Pfad für einen JSON-Bericht. Standard: odoo-api-report-<host>-<datum>.json im aktuellen Ordner.

.PARAMETER ExtraPaths
    Zusätzliche Pfade, die per GET angefragt werden (z.B. REST-Module von Drittanbietern).

.EXAMPLE
    .\Test-OdooApi.ps1 -Url https://erp.firma.de -NoAuth

.EXAMPLE
    .\Test-OdooApi.ps1 -Url https://erp.firma.de -Database prod -User admin@firma.de

.EXAMPLE
    $key = Read-Host -AsSecureString "API-Key"
    .\Test-OdooApi.ps1 -Url https://erp.firma.de -Database prod -User importer@firma.de -ApiKey $key
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [string]$Database,
    [string]$User,
    [SecureString]$ApiKey,
    [switch]$NoAuth,
    [switch]$SkipCertificateCheck,
    [int]$TimeoutSec = 30,
    [string]$OutFile,
    [string[]]$ExtraPaths = @('/api', '/api/v1', '/restapi/1.0/common/version', '/graphql', '/doc')
)

$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -ne 'Core') {
    # Windows PowerShell 5.1: verhindert {"value":[...],"Count":n} in ConvertTo-Json
    Remove-TypeData System.Array -ErrorAction SilentlyContinue
}
$script:BaseUrl = $Url.TrimEnd('/')
$script:Results = New-Object System.Collections.ArrayList
$script:Facts = [ordered]@{}

# --------------------------------------------------------------------------------------
# Ausgabe
# --------------------------------------------------------------------------------------
function Add-Result {
    param([string]$Group, [string]$Name, [ValidateSet('OK', 'WARN', 'FAIL', 'INFO', 'SKIP')][string]$Status, [string]$Detail)
    $color = @{ OK = 'Green'; WARN = 'Yellow'; FAIL = 'Red'; INFO = 'Cyan'; SKIP = 'DarkGray' }[$Status]
    Write-Host ('  [{0,-4}] ' -f $Status) -ForegroundColor $color -NoNewline
    Write-Host ('{0,-38} {1}' -f $Name, $Detail)
    [void]$script:Results.Add([pscustomobject]@{ Group = $Group; Check = $Name; Status = $Status; Detail = $Detail })
}

function Write-Section([string]$Title) {
    Write-Host ''
    Write-Host "== $Title ==" -ForegroundColor White
}

# --------------------------------------------------------------------------------------
# HTTP (HttpClient: identisch in PS 5.1 und 7, wirft bei 4xx/5xx keine Ausnahme)
# --------------------------------------------------------------------------------------
function Initialize-Http {
    if ($PSVersionTable.PSEdition -ne 'Core') {
        Add-Type -AssemblyName System.Net.Http
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    }
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseCookies = $false
    $handler.AllowAutoRedirect = $true
    if ($SkipCertificateCheck) {
        if ($PSVersionTable.PSEdition -eq 'Core') {
            $handler.ServerCertificateCustomValidationCallback = [System.Net.Http.HttpClientHandler]::DangerousAcceptAnyServerCertificateValidator
        }
        else {
            if (-not ('OdooApiProbe.TrustAll' -as [type])) {
                Add-Type -TypeDefinition @'
namespace OdooApiProbe {
    public static class TrustAll {
        public static bool Validate(object s, System.Security.Cryptography.X509Certificates.X509Certificate c,
            System.Security.Cryptography.X509Certificates.X509Chain ch, System.Net.Security.SslPolicyErrors e) { return true; }
    }
}
'@
            }
            [Net.ServicePointManager]::ServerCertificateValidationCallback = [Delegate]::CreateDelegate(
                [Net.Security.RemoteCertificateValidationCallback], [OdooApiProbe.TrustAll].GetMethod('Validate'))
        }
    }
    $script:Http = New-Object System.Net.Http.HttpClient($handler)
    $script:Http.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
    $script:Http.DefaultRequestHeaders.TryAddWithoutValidation('User-Agent', 'Odoo-API-Probe/1.0 (PowerShell)') | Out-Null
}

function Invoke-Http {
    param([string]$Method, [string]$Path, [string]$Body, [string]$ContentType = 'application/json', [hashtable]$Headers = @{})
    $req = New-Object System.Net.Http.HttpRequestMessage ([System.Net.Http.HttpMethod]::new($Method)), ($script:BaseUrl + $Path)
    if ($null -ne $Body -and $Body -ne '') {
        $req.Content = New-Object System.Net.Http.StringContent($Body, [Text.Encoding]::UTF8, $ContentType)
    }
    foreach ($k in $Headers.Keys) { [void]$req.Headers.TryAddWithoutValidation($k, [string]$Headers[$k]) }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = $script:Http.SendAsync($req).GetAwaiter().GetResult()
        $text = $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        $ct = if ($resp.Content.Headers.ContentType) { $resp.Content.Headers.ContentType.MediaType } else { '' }
        return [pscustomobject]@{
            Status = [int]$resp.StatusCode; Body = $text; ContentType = $ct; Ms = $sw.ElapsedMilliseconds
            Server = ($resp.Headers.Server | ForEach-Object { $_.ToString() }) -join ' '
            FinalUrl = [string]$resp.RequestMessage.RequestUri; Error = $null
        }
    }
    catch {
        return [pscustomobject]@{
            Status = 0; Body = $null; ContentType = ''; Ms = $sw.ElapsedMilliseconds; Server = ''; FinalUrl = ''
            Error = $_.Exception.GetBaseException().Message
        }
    }
}

function Get-ShortError([string]$Text) {
    if (-not $Text) { return '' }
    $lines = @($Text -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $last = $lines[-1]
    if ($last.Length -gt 200) { $last = $last.Substring(0, 200) + '...' }
    return $last
}

# --------------------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------------------
function Invoke-JsonRpc {
    param([string]$Path, [hashtable]$Params = @{})
    $payload = @{ jsonrpc = '2.0'; method = 'call'; params = $Params; id = (Get-Random -Maximum 100000) } |
        ConvertTo-Json -Depth 30 -Compress
    $r = Invoke-Http -Method POST -Path $Path -Body $payload -ContentType 'application/json'
    $out = [pscustomobject]@{ Ok = $false; Result = $null; Error = $null; Status = $r.Status; Ms = $r.Ms }
    if ($r.Error) { $out.Error = $r.Error; return $out }
    try { $json = $r.Body | ConvertFrom-Json }
    catch { $out.Error = "Keine JSON-Antwort (HTTP $($r.Status))"; return $out }
    if ($json.PSObject.Properties.Name -contains 'error' -and $json.error) {
        $msg = $json.error.message
        if ($json.error.data -and $json.error.data.message) { $msg = $json.error.data.message }
        $out.Error = Get-ShortError $msg
        return $out
    }
    $out.Ok = $true
    $out.Result = $json.result
    return $out
}

# --------------------------------------------------------------------------------------
# XML-RPC (minimaler Serializer/Parser)
# --------------------------------------------------------------------------------------
function ConvertTo-XmlRpcValue($v) {
    if ($null -eq $v) { return '<value><nil/></value>' }
    if ($v -is [bool]) { return "<value><boolean>$([int]$v)</boolean></value>" }
    if ($v -is [int] -or $v -is [long] -or $v -is [int16]) { return "<value><int>$v</int></value>" }
    if ($v -is [double] -or $v -is [decimal] -or $v -is [single]) {
        return "<value><double>$($v.ToString([Globalization.CultureInfo]::InvariantCulture))</double></value>"
    }
    if ($v -is [string]) { return "<value><string>$([Security.SecurityElement]::Escape($v))</string></value>" }
    if ($v -is [System.Collections.IDictionary]) {
        $sb = New-Object Text.StringBuilder '<value><struct>'
        foreach ($k in $v.Keys) {
            [void]$sb.Append("<member><name>$([Security.SecurityElement]::Escape([string]$k))</name>")
            [void]$sb.Append((ConvertTo-XmlRpcValue $v[$k]))
            [void]$sb.Append('</member>')
        }
        [void]$sb.Append('</struct></value>')
        return $sb.ToString()
    }
    if ($v -is [System.Collections.IEnumerable]) {
        $sb = New-Object Text.StringBuilder '<value><array><data>'
        foreach ($item in $v) { [void]$sb.Append((ConvertTo-XmlRpcValue $item)) }
        [void]$sb.Append('</data></array></value>')
        return $sb.ToString()
    }
    return "<value><string>$([Security.SecurityElement]::Escape([string]$v))</string></value>"
}

function ConvertFrom-XmlRpcValue($node) {
    $child = $node.ChildNodes | Where-Object { $_.NodeType -eq 'Element' } | Select-Object -First 1
    if (-not $child) { return [string]$node.InnerText }
    switch ($child.LocalName) {
        { $_ -in 'int', 'i4', 'i8' } { return [long]$child.InnerText }
        'boolean' { return ($child.InnerText -eq '1') }
        'double' { return [double]::Parse($child.InnerText, [Globalization.CultureInfo]::InvariantCulture) }
        'nil' { return $null }
        'struct' {
            $h = [ordered]@{}
            foreach ($m in $child.SelectNodes('member')) {
                $h[$m.SelectSingleNode('name').InnerText] = ConvertFrom-XmlRpcValue $m.SelectSingleNode('value')
            }
            return $h
        }
        'array' {
            $list = New-Object System.Collections.ArrayList
            foreach ($val in $child.SelectNodes('data/value')) { [void]$list.Add((ConvertFrom-XmlRpcValue $val)) }
            return , $list.ToArray()
        }
        default { return [string]$child.InnerText }
    }
}

function Invoke-XmlRpc {
    param([string]$Path, [string]$MethodName, [object[]]$Params = @())
    $sb = New-Object Text.StringBuilder "<?xml version='1.0'?><methodCall><methodName>$MethodName</methodName><params>"
    foreach ($p in $Params) { [void]$sb.Append('<param>' + (ConvertTo-XmlRpcValue $p) + '</param>') }
    [void]$sb.Append('</params></methodCall>')
    $r = Invoke-Http -Method POST -Path $Path -Body $sb.ToString() -ContentType 'text/xml'
    $out = [pscustomobject]@{ Ok = $false; Result = $null; Error = $null; Status = $r.Status; Ms = $r.Ms }
    if ($r.Error) { $out.Error = $r.Error; return $out }
    try { [xml]$doc = $r.Body }
    catch { $out.Error = "Keine XML-RPC-Antwort (HTTP $($r.Status))"; return $out }
    $fault = $doc.SelectSingleNode('/methodResponse/fault/value')
    if ($fault) {
        $f = ConvertFrom-XmlRpcValue $fault
        $out.Error = Get-ShortError ([string]$f['faultString'])
        return $out
    }
    $val = $doc.SelectSingleNode('/methodResponse/params/param/value')
    if (-not $val) { $out.Error = "Unerwartete Antwort (HTTP $($r.Status))"; return $out }
    $out.Ok = $true
    $out.Result = ConvertFrom-XmlRpcValue $val
    return $out
}

# --------------------------------------------------------------------------------------
# Odoo-Aufruf über die erste funktionierende RPC-Variante
# --------------------------------------------------------------------------------------
function Invoke-OdooExecute {
    param([string]$Model, [string]$Method, [object[]]$MethodArgs = @(), [hashtable]$Kwargs = @{})
    $allArgs = @($script:Db, $script:Uid, $script:Secret, $Model, $Method, $MethodArgs, $Kwargs)
    if ($script:Facts['xmlrpc_login']) {
        return Invoke-XmlRpc -Path '/xmlrpc/2/object' -MethodName 'execute_kw' -Params $allArgs
    }
    return Invoke-JsonRpc -Path '/jsonrpc' -Params @{ service = 'object'; method = 'execute_kw'; args = $allArgs }
}

function Test-ModelAccess([string]$Model, [string]$Operation) {
    # Odoo <= 18: check_access_rights (model method), Odoo >= 18: has_access (recordset method)
    $r = Invoke-OdooExecute -Model $Model -Method 'check_access_rights' -MethodArgs @($Operation) -Kwargs @{ raise_exception = $false }
    if ($r.Ok) { return $r }
    $r2 = Invoke-OdooExecute -Model $Model -Method 'has_access' -MethodArgs @(@(), $Operation)
    if ($r2.Ok) { return $r2 }
    return $r
}

# ======================================================================================
Initialize-Http
$uri = [Uri]$script:BaseUrl
Write-Host "Odoo-API-Prüfung für $($script:BaseUrl)   ($(Get-Date -Format 'yyyy-MM-dd HH:mm'), PowerShell $($PSVersionTable.PSVersion))" -ForegroundColor White
Write-Host 'Hinweis: Es werden ausschließlich lesende Aufrufe ausgeführt.' -ForegroundColor DarkGray

# --------------------------------------------------------------------------------------
Write-Section 'Erreichbarkeit & Version'
$r = Invoke-Http -Method GET -Path '/web/login'
if ($r.Error) {
    Add-Result 'Basis' 'HTTP erreichbar' 'FAIL' $r.Error
    Write-Host ''
    Write-Host 'Server nicht erreichbar - Abbruch.' -ForegroundColor Red
    exit 2
}
$detail = "HTTP $($r.Status), $($r.Ms) ms"
if ($r.Server) { $detail += ", Server: $($r.Server)" }
Add-Result 'Basis' 'HTTP erreichbar (/web/login)' $(if ($r.Status -lt 400) { 'OK' } else { 'WARN' }) $detail
if ($uri.Scheme -ne 'https') { Add-Result 'Basis' 'Verschlüsselung' 'WARN' 'Kein HTTPS - Zugangsdaten werden unverschlüsselt übertragen' }
if ($script:BaseUrl -match '\.odoo\.com$') {
    Add-Result 'Basis' 'Odoo Online (SaaS)' 'INFO' 'Externe API ist bei Odoo Online nur im Tarif "Custom" freigeschaltet'
}

$vi = Invoke-JsonRpc -Path '/web/webclient/version_info'
$major = 0
if ($vi.Ok) {
    $script:Facts['server_version'] = [string]$vi.Result.server_version
    $serie = [string]$vi.Result.server_serie
    $script:Facts['server_serie'] = $serie
    if ($serie -match '^(saas~)?(\d+)') { $major = [int]$Matches[2] }
    $script:Facts['major_version'] = $major
    Add-Result 'Basis' 'Odoo-Version' 'INFO' ("{0} (Serie {1}, Protokoll {2})" -f $vi.Result.server_version, $serie, $vi.Result.protocol_version)
}
else {
    Add-Result 'Basis' 'Odoo-Version' 'WARN' "version_info nicht abrufbar: $($vi.Error)"
}

# --------------------------------------------------------------------------------------
Write-Section 'API-Endpunkte (anonym)'
$x = Invoke-XmlRpc -Path '/xmlrpc/2/common' -MethodName 'version'
$script:Facts['xmlrpc'] = $x.Ok
if ($x.Ok) {
    Add-Result 'API' 'XML-RPC  /xmlrpc/2' 'OK' "verfügbar ($($x.Ms) ms), server_version=$($x.Result['server_version'])"
    if (-not $major -and [string]$x.Result['server_serie'] -match '^(saas~)?(\d+)') { $major = [int]$Matches[2]; $script:Facts['major_version'] = $major }
}
else { Add-Result 'API' 'XML-RPC  /xmlrpc/2' 'FAIL' "HTTP $($x.Status): $($x.Error)" }

$j = Invoke-JsonRpc -Path '/jsonrpc' -Params @{ service = 'common'; method = 'version'; args = @() }
$script:Facts['jsonrpc'] = $j.Ok
if ($j.Ok) { Add-Result 'API' 'JSON-RPC /jsonrpc' 'OK' "verfügbar ($($j.Ms) ms)" }
else { Add-Result 'API' 'JSON-RPC /jsonrpc' 'FAIL' "HTTP $($j.Status): $($j.Error)" }

if ($major -ge 19 -and ($x.Ok -or $j.Ok)) {
    Add-Result 'API' 'XML-RPC/JSON-RPC Zukunft' 'WARN' 'Ab Odoo 19 als veraltet markiert, Entfernung angekündigt -> JSON-2 bevorzugen'
}

# JSON-2 (Odoo 19+): ohne Schlüssel erwarten wir 401, fehlt die Route gibt es 404
$j2 = Invoke-Http -Method POST -Path '/json/2/res.partner/search_count' -Body '{"domain": []}' -ContentType 'application/json'
$script:Facts['json2_endpoint'] = $false
if ($j2.Error) { Add-Result 'API' 'JSON-2   /json/2' 'FAIL' $j2.Error }
elseif ($j2.Status -eq 404) { Add-Result 'API' 'JSON-2   /json/2' 'INFO' 'nicht vorhanden (gibt es erst ab Odoo 19)' }
elseif ($j2.Status -in 400, 401, 403 -and $j2.ContentType -match 'json') {
    $script:Facts['json2_endpoint'] = $true
    Add-Result 'API' 'JSON-2   /json/2' 'OK' "Endpunkt vorhanden (HTTP $($j2.Status) ohne API-Key - erwartet)"
}
elseif ($j2.FinalUrl -match '/web/login') {
    # Odoo 17/18 haben unter /json/ nur die JSON-Ansicht des Webclients (Login-Umleitung)
    Add-Result 'API' 'JSON-2   /json/2' 'INFO' 'nicht vorhanden (gibt es erst ab Odoo 19)'
}
else { Add-Result 'API' 'JSON-2   /json/2' 'WARN' "unerwartete Antwort HTTP $($j2.Status)" }

$dbl = Invoke-JsonRpc -Path '/web/database/list'
if ($dbl.Ok) {
    $dbs = @($dbl.Result)
    $script:Facts['databases'] = $dbs
    Add-Result 'API' 'Datenbank-Liste' 'WARN' ("öffentlich sichtbar: {0} (Tipp: list_db = False setzen)" -f ($dbs -join ', '))
    if (-not $Database -and $dbs.Count -eq 1) { $Database = [string]$dbs[0]; Add-Result 'API' 'Datenbank gewählt' 'INFO' $Database }
}
else { Add-Result 'API' 'Datenbank-Liste' 'OK' 'nicht öffentlich (list_db deaktiviert)' }

foreach ($p in $ExtraPaths) {
    $e = Invoke-Http -Method GET -Path $p
    if ($e.Error) { Add-Result 'Extra' "GET $p" 'INFO' $e.Error }
    elseif ($e.Status -eq 404) { Add-Result 'Extra' "GET $p" 'INFO' 'nicht vorhanden (404)' }
    else { Add-Result 'Extra' "GET $p" 'INFO' "HTTP $($e.Status) ($($e.ContentType)) -> manuell prüfen" }
}

# --------------------------------------------------------------------------------------
$authOk = $false
if ($NoAuth) {
    Write-Section 'Anmeldung'
    Add-Result 'Auth' 'Anmeldung' 'SKIP' '-NoAuth gesetzt'
}
else {
    Write-Section 'Anmeldung'
    if (-not $Database) { $Database = Read-Host 'Odoo-Datenbankname' }
    if (-not $User) { $User = Read-Host 'Odoo-Login (E-Mail)' }
    if (-not $ApiKey) {
        if ($env:ODOO_API_KEY) { $ApiKey = ConvertTo-SecureString $env:ODOO_API_KEY -AsPlainText -Force }
        else { $ApiKey = Read-Host -AsSecureString 'API-Schlüssel (oder Passwort)' }
    }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ApiKey)
    try { $script:Secret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $script:Db = $Database
    $script:Facts['database'] = $Database
    $script:Facts['user'] = $User

    if ($script:Facts['xmlrpc']) {
        $a = Invoke-XmlRpc -Path '/xmlrpc/2/common' -MethodName 'authenticate' -Params @($Database, $User, $script:Secret, @{})
        if ($a.Ok -and $a.Result -is [long] -and $a.Result -gt 0) {
            $script:Uid = [int]$a.Result; $script:Facts['xmlrpc_login'] = $true
            Add-Result 'Auth' 'Login XML-RPC' 'OK' "uid=$($script:Uid) ($($a.Ms) ms)"
        }
        else { Add-Result 'Auth' 'Login XML-RPC' 'FAIL' $(if ($a.Error) { $a.Error } else { 'Zugangsdaten abgelehnt' }) }
    }
    if ($script:Facts['jsonrpc']) {
        $a = Invoke-JsonRpc -Path '/jsonrpc' -Params @{ service = 'common'; method = 'login'; args = @($Database, $User, $script:Secret) }
        if ($a.Ok -and $a.Result -and [int]$a.Result -gt 0) {
            $script:Uid = [int]$a.Result; $script:Facts['jsonrpc_login'] = $true
            Add-Result 'Auth' 'Login JSON-RPC' 'OK' "uid=$($script:Uid) ($($a.Ms) ms)"
        }
        else { Add-Result 'Auth' 'Login JSON-RPC' 'FAIL' $(if ($a.Error) { $a.Error } else { 'Zugangsdaten abgelehnt' }) }
    }
    if ($script:Facts['json2_endpoint']) {
        $h = @{ Authorization = "bearer $($script:Secret)"; 'X-Odoo-Database' = $Database }
        $a = Invoke-Http -Method POST -Path '/json/2/res.partner/search_count' -Body '{"domain": []}' -ContentType 'application/json' -Headers $h
        if ($a.Status -eq 200) {
            $script:Facts['json2_login'] = $true
            Add-Result 'Auth' 'Login JSON-2 (Bearer)' 'OK' "search_count res.partner = $($a.Body) ($($a.Ms) ms)"
        }
        else {
            $msg = try { ($a.Body | ConvertFrom-Json).message } catch { $a.Body }
            Add-Result 'Auth' 'Login JSON-2 (Bearer)' 'FAIL' ("HTTP {0}: {1} (JSON-2 braucht einen API-Schlüssel, kein Passwort)" -f $a.Status, (Get-ShortError ([string]$msg)))
        }
    }
    $authOk = [bool]($script:Facts['xmlrpc_login'] -or $script:Facts['jsonrpc_login'])
}

# --------------------------------------------------------------------------------------
if ($authOk) {
    Write-Section 'Benutzer & Module'
    $u = Invoke-OdooExecute -Model 'res.users' -Method 'read' -MethodArgs @(@($script:Uid)) -Kwargs @{ fields = @('name', 'login', 'share', 'lang', 'tz') }
    if ($u.Ok) {
        $rec = @($u.Result)[0]
        $nm = if ($rec -is [System.Collections.IDictionary]) { $rec['name'] } else { $rec.name }
        $lg = if ($rec -is [System.Collections.IDictionary]) { $rec['login'] } else { $rec.login }
        Add-Result 'User' 'Benutzer' 'INFO' "$nm <$lg>"
    }
    foreach ($g in @(@('base.group_system', 'Administrator (Einstellungen)'), @('sales_team.group_sale_salesman', 'Vertrieb: eigene Dokumente'), @('sales_team.group_sale_manager', 'Vertrieb: Administrator'))) {
        $hg = Invoke-OdooExecute -Model 'res.users' -Method 'has_group' -MethodArgs @(@($script:Uid), $g[0])
        if ($hg.Ok) { Add-Result 'User' "Gruppe $($g[1])" $(if ($hg.Result) { 'OK' } else { 'INFO' }) $(if ($hg.Result) { 'ja' } else { 'nein' }) }
        else { Add-Result 'User' "Gruppe $($g[1])" 'WARN' $hg.Error }
    }

    $wanted = @('crm', 'mail', 'contacts', 'sale_crm', 'mass_mailing', 'website_crm')
    $m = Invoke-OdooExecute -Model 'ir.module.module' -Method 'search_read' `
        -MethodArgs @(, @(, @('name', 'in', $wanted))) -Kwargs @{ fields = @('name', 'state', 'latest_version') }
    if ($m.Ok) {
        $byName = @{}
        foreach ($rec in @($m.Result)) {
            $n = if ($rec -is [System.Collections.IDictionary]) { $rec['name'] } else { $rec.name }
            $byName[$n] = $rec
        }
        $script:Facts['modules'] = @{}
        foreach ($w in $wanted) {
            $rec = $byName[$w]
            $state = if ($rec) { if ($rec -is [System.Collections.IDictionary]) { $rec['state'] } else { $rec.state } } else { 'nicht vorhanden' }
            $script:Facts['modules'][$w] = $state
            $status = if ($state -eq 'installed') { 'OK' } elseif ($w -in 'crm', 'mail', 'contacts') { 'FAIL' } else { 'INFO' }
            Add-Result 'Module' "Modul $w" $status $state
        }
    }
    else { Add-Result 'Module' 'Installierte Module' 'WARN' "nicht lesbar: $($m.Error)" }

    Write-Section 'Zugriffsrechte (für den Import benötigt)'
    $need = [ordered]@{
        'res.partner'   = @('read', 'create', 'write')
        'crm.lead'      = @('read', 'create', 'write')
        'crm.tag'       = @('read', 'create')
        'mail.message'  = @('read', 'create')
        'ir.attachment' = @('read', 'create')
    }
    $script:Facts['access'] = [ordered]@{}
    foreach ($model in $need.Keys) {
        $res = @()
        $bad = @()
        foreach ($op in $need[$model]) {
            $acc = Test-ModelAccess $model $op
            if ($acc.Ok -and $acc.Result) { $res += $op } else { $bad += $op }
        }
        $script:Facts['access'][$model] = @{ granted = $res; missing = $bad }
        if ($bad.Count -eq 0) { Add-Result 'Rechte' $model 'OK' ($res -join ', ') }
        else { Add-Result 'Rechte' $model 'FAIL' ("fehlt: {0}" -f ($bad -join ', ')) }
    }

    Write-Section 'Mengengerüst & Felder'
    $counts = [ordered]@{
        'Kontakte (res.partner)'           = @('res.partner', @())
        'Leads/Chancen inkl. archiviert'   = @('crm.lead', @())
        'Nachrichten an Leads'             = @('mail.message', @(, @('model', '=', 'crm.lead')))
        'E-Mails in Odoo (message_type)'   = @('mail.message', @(, @('message_type', '=', 'email')))
        'Eingehende Mailserver (fetchmail)' = @('fetchmail.server', @())
    }
    $script:Facts['counts'] = [ordered]@{}
    foreach ($label in $counts.Keys) {
        $model = $counts[$label][0]; $domain = $counts[$label][1]
        $c = Invoke-OdooExecute -Model $model -Method 'search_count' -MethodArgs @(, $domain) -Kwargs @{ context = @{ active_test = $false } }
        if ($c.Ok) { $script:Facts['counts'][$label] = $c.Result; Add-Result 'Daten' $label 'INFO' ([string]$c.Result) }
        else { Add-Result 'Daten' $label 'INFO' "nicht lesbar: $($c.Error)" }
    }

    $fieldChecks = [ordered]@{
        'crm.lead'     = @('email_from', 'email_normalized', 'partner_id', 'contact_name', 'partner_name', 'phone', 'description', 'tag_ids', 'team_id', 'type', 'probability', 'active')
        'res.partner'  = @('email', 'email_normalized', 'is_company', 'parent_id', 'phone')
        'mail.message' = @('message_id', 'model', 'res_id', 'message_type', 'subtype_id', 'author_id', 'email_from', 'date', 'attachment_ids')
    }
    foreach ($model in $fieldChecks.Keys) {
        $fg = Invoke-OdooExecute -Model $model -Method 'fields_get' -Kwargs @{ attributes = @('type') }
        if (-not $fg.Ok) { Add-Result 'Felder' $model 'WARN' $fg.Error; continue }
        $names = if ($fg.Result -is [System.Collections.IDictionary]) { @($fg.Result.Keys) } else { @($fg.Result.PSObject.Properties.Name) }
        $missing = @($fieldChecks[$model] | Where-Object { $names -notcontains $_ })
        if ($missing.Count -eq 0) { Add-Result 'Felder' $model 'OK' 'alle benötigten Felder vorhanden' }
        else { Add-Result 'Felder' $model 'INFO' ("nicht vorhanden: {0}" -f ($missing -join ', ')) }
    }

    Write-Section 'Performance'
    $times = @()
    for ($i = 0; $i -lt 5; $i++) {
        $p = Invoke-OdooExecute -Model 'res.partner' -Method 'search_count' -MethodArgs @(, @())
        if ($p.Ok) { $times += $p.Ms }
    }
    if ($times.Count) {
        $avg = [math]::Round(($times | Measure-Object -Average).Average)
        $script:Facts['latency_ms'] = $avg
        # pro importierter Mail grob 4-8 Aufrufe (Suche, Partner, Lead, Nachricht, Anhänge)
        $perHour = [math]::Floor(3600000 / [math]::Max(1, $avg * 6))
        Add-Result 'Perf' 'Latenz pro RPC-Aufruf' $(if ($avg -lt 300) { 'OK' } else { 'WARN' }) "Ø $avg ms (5 Aufrufe) -> grob $perHour Mails/Stunde sequentiell"
    }
}
elseif (-not $NoAuth) {
    Write-Section 'Benutzer & Module'
    Add-Result 'Auth' 'Weitere Prüfungen' 'SKIP' 'keine erfolgreiche Anmeldung'
}

# --------------------------------------------------------------------------------------
Write-Section 'Ergebnis'
$rec = 'keine nutzbare API gefunden'
if ($script:Facts['json2_login']) { $rec = 'JSON-2 (/json/2, Bearer API-Key)' }
elseif ($authOk -and $script:Facts['xmlrpc_login']) { $rec = 'XML-RPC (/xmlrpc/2) mit API-Schlüssel' }
elseif ($authOk) { $rec = 'JSON-RPC (/jsonrpc) mit API-Schlüssel' }
elseif ($script:Facts['json2_endpoint']) { $rec = 'JSON-2 vorhanden - mit API-Schlüssel erneut prüfen' }
elseif ($script:Facts['xmlrpc'] -or $script:Facts['jsonrpc']) { $rec = 'XML-RPC/JSON-RPC vorhanden - mit Zugangsdaten erneut prüfen' }
$script:Facts['recommendation'] = $rec
Write-Host "  Empfohlene Schnittstelle für den Importer: $rec" -ForegroundColor White
$fails = @($script:Results | Where-Object Status -eq 'FAIL').Count
$warns = @($script:Results | Where-Object Status -eq 'WARN').Count
Write-Host ("  {0} Fehler, {1} Warnungen" -f $fails, $warns) -ForegroundColor $(if ($fails) { 'Red' } elseif ($warns) { 'Yellow' } else { 'Green' })

if (-not $OutFile) { $OutFile = "odoo-api-report-$($uri.Host)-$(Get-Date -Format 'yyyyMMdd-HHmm').json" }
$report = [ordered]@{
    url = $script:BaseUrl; checked_at = (Get-Date).ToString('s'); powershell = $PSVersionTable.PSVersion.ToString()
    facts = $script:Facts; results = $script:Results
}
$report | ConvertTo-Json -Depth 10 | Set-Content -Path $OutFile -Encoding UTF8
Write-Host "  Bericht gespeichert: $OutFile (enthält keine Passwörter/Schlüssel)" -ForegroundColor DarkGray

$script:Secret = $null
if ($authOk -or $script:Facts['json2_login']) { exit 0 }
if ($NoAuth -and ($script:Facts['xmlrpc'] -or $script:Facts['jsonrpc'] -or $script:Facts['json2_endpoint'])) { exit 0 }
exit 1
