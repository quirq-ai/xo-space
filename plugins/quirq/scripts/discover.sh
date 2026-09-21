#!/usr/bin/env bash
# Read-only local discovery. Requires only bash, curl, and POSIX awk.
# A pointer is a last-known-location hint; discovery never installs or starts
# anything. stdout contains exactly one JSON object (running, installed, or
# not_installed). QUIRQ_DISCOVER_PORTS replaces the fallback ports, not the
# pointer port. Only verified pointer-port discoveries inherit pointer paths.
set -uf
export LC_ALL=C

POINTER_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/quirq/install.json"
CURL_TIMEOUT=2

# Read a top-level string or number, validating the entire JSON document.
# Unlike grep extraction, this handles escaped quotes, backslashes, and the
# Unicode escapes emitted by Python json.dumps. Do not evaluate pointer data.
json_value() { # $1=field $2=type; JSON on stdin
    awk -v field="$1" -v expected="$2" '
    function fail() { invalid=1; exit 1 }
    function ws() { while (substr(doc,pos,1) ~ /^[ \t\r\n]$/) pos++ }
    function hex4(    text,i,n,digit) {
        text=substr(doc,pos,4)
        if (length(text)!=4 || text ~ /[^0-9a-fA-F]/) fail()
        pos+=4; n=0
        for(i=1;i<=4;i++) {
            digit=index("0123456789abcdef",tolower(substr(text,i,1)))-1
            n=n*16+digit
        }
        return n
    }
    function utf8(n) {
        if (n<128) return sprintf("%c",n)
        if (n<2048) return sprintf("%c%c",192+int(n/64),128+n%64)
        if (n<65536) return sprintf("%c%c%c",224+int(n/4096),128+int(n/64)%64,128+n%64)
        return sprintf("%c%c%c%c",240+int(n/262144),128+int(n/4096)%64,128+int(n/64)%64,128+n%64)
    }
    function string(    out,c,e,n,low) {
        if(substr(doc,pos++,1)!="\"") fail()
        out=""
        while(pos<=length(doc)) {
            c=substr(doc,pos++,1)
            if(c=="\"") { value=out; kind="string"; return }
            if(c ~ /[[:cntrl:]]/) fail()
            if(c=="\\") {
                e=substr(doc,pos++,1)
                if(e=="\"" || e=="\\" || e=="/") c=e
                else if(e=="b") c=sprintf("%c",8)
                else if(e=="f") c=sprintf("%c",12)
                else if(e=="n") c="\n"
                else if(e=="r") c="\r"
                else if(e=="t") c="\t"
                else if(e=="u") {
                    n=hex4()
                    if(n>=55296 && n<=56319) {
                        if(substr(doc,pos,2)!="\\u") fail()
                        pos+=2; low=hex4()
                        if(low<56320 || low>57343) fail()
                        n=65536+(n-55296)*1024+low-56320
                    } else if(n>=56320 && n<=57343) fail()
                    # Shell variables and filesystem paths cannot contain NUL.
                    if(n==0) fail()
                    c=utf8(n)
                } else fail()
            }
            out=out c
        }
        fail()
    }
    function parse(level,    c,key,token) {
        if(level>64) fail()
        ws(); c=substr(doc,pos,1)
        if(c=="\"") { string(); return }
        if(c=="{" || c=="[") {
            pos++; ws()
            if(substr(doc,pos,1)==(c=="{"?"}":"]")) { pos++; kind="container"; return }
            while(1) {
                if(c=="{") {
                    string(); key=value; ws()
                    if(substr(doc,pos++,1)!=":") fail()
                }
                parse(level+1)
                if(level==0 && c=="{" && key==field) {
                    if(found++) fail()
                    selected=value; selected_kind=kind
                }
                ws(); token=substr(doc,pos++,1)
                if(token==(c=="{"?"}":"]")) break
                if(token!=",") fail()
                ws()
            }
            kind="container"; return
        }
        if(match(substr(doc,pos),/^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?/)) {
            value=substr(doc,pos,RLENGTH); pos+=RLENGTH; kind="number"; return
        }
        if(match(substr(doc,pos),/^(true|false|null)/)) {
            pos+=RLENGTH; kind="literal"; return
        }
        fail()
    }
    { doc=doc $0 "\n" }
    END {
        if(invalid) exit 1
        pos=1; ws()
        if(substr(doc,pos,1)!="{") fail()
        parse(0); ws()
        if(pos<=length(doc) || !found || selected_kind!=expected) fail()
        printf "%s",selected
    }'
}

# Assign via a sentinel so command substitution preserves trailing newlines.
read_json() { # $1=JSON $2=field $3=type; result in JSON_VALUE
    JSON_VALUE="$(printf '%s' "$1" | json_value "$2" "$3" && printf '.')" || return 1
    JSON_VALUE="${JSON_VALUE%.}"
}

json_string() { # JSON-escape arbitrary path bytes, including control bytes.
    local value="$1" i char code
    printf '"'
    for ((i=0; i<${#value}; i++)); do
        char="${value:i:1}"
        case "$char" in
            '"') printf '\\"' ;;
            '\') printf '\\\\' ;;
            *) printf -v code '%d' "'$char"
               if [ "$code" -ge 0 ] && [ "$code" -lt 32 ]; then printf '\\u%04x' "$code"; else printf '%s' "$char"; fi ;;
        esac
    done
    printf '"'
}

emit() { # state base_url repo_dir projects_root state_root
    local key value
    printf '{'
    for key in state base_url repo_dir projects_root state_root pointer_file; do
        if [ "$key" = pointer_file ]; then value="$POINTER_FILE"; else value="$1"; shift; fi
        printf '"%s":' "$key"; json_string "$value"
        [ "$key" = pointer_file ] || printf ','
    done
    printf '}\n'
}

valid_port() {
    case "$1" in ''|*[!0-9]*|0*) return 1 ;; esac
    [ "${#1}" -le 5 ] && [ "$1" -le 65535 ]
}

checkout_exists() {
    [ -f "$1/server.py" ] && [ -f "$1/requirements.txt" ] && [ -f "$1/cowork-api.sh" ]
}

probe_health() { # A generic healthy service must not be mistaken for Quirq.
    local body
    body="$(curl --noproxy '*' -fsS -m "$CURL_TIMEOUT" "http://127.0.0.1:$1/health" 2>/dev/null)" || return 1
    read_json "$body" status string && [ "$JSON_VALUE" = healthy ] || return 1
    body="$(curl --noproxy '*' -fsS -m "$CURL_TIMEOUT" -H 'Accept: application/json' "http://127.0.0.1:$1/" 2>/dev/null)" || return 1
    read_json "$body" status string && [ "$JSON_VALUE" = 'XO Space API running' ]
}

pointer_repo=""
pointer_projects=""
pointer_state=""
pointer_port=""
if [ -f "$POINTER_FILE" ]; then
    pointer_json="$(cat "$POINTER_FILE")"
    if read_json "$pointer_json" repo_dir string; then pointer_repo="$JSON_VALUE"; fi
    if read_json "$pointer_json" projects_root string; then pointer_projects="$JSON_VALUE"; fi
    if read_json "$pointer_json" state_root string; then pointer_state="$JSON_VALUE"; fi
    if read_json "$pointer_json" port number && valid_port "$JSON_VALUE"; then pointer_port="$JSON_VALUE"; fi
fi

# Check the pointer port before fallback ports, ignoring malformed candidates.
candidate_ports="$pointer_port ${QUIRQ_DISCOVER_PORTS:-5002 5003}"
seen_ports=" "
for port in $candidate_ports; do
    valid_port "$port" || continue
    case "$seen_ports" in *" $port "*) continue ;; esac
    seen_ports="$seen_ports$port "
    if probe_health "$port"; then
        if [ "$port" = "$pointer_port" ] && checkout_exists "$pointer_repo"; then
            emit running "http://127.0.0.1:$port" "$pointer_repo" "$pointer_projects" "$pointer_state"
        else
            emit running "http://127.0.0.1:$port" "" "" ""
        fi
        exit 0
    fi
done

if checkout_exists "$pointer_repo"; then
    emit installed "" "$pointer_repo" "$pointer_projects" "$pointer_state"
    exit 0
fi
for dir in "$PWD/xo-space" "$PWD"; do
    if checkout_exists "$dir"; then emit installed "" "$dir" "" ""; exit 0; fi
done
emit not_installed "" "" "" ""
