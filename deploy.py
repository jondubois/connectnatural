#!/usr/bin/env python3
"""Deploy the frontend to Saasufy's file hosting.

Rewrites the source from the development environment to the production one and
uploads the result to the record which serves it. The source file on disk is
never modified, so local development keeps its dev URLs and `keycloak:dev`
OAuth provider.

    ./deploy.py             # rewrite and upload
    ./deploy.py --dry-run   # report the rewrites, upload nothing

`deployURL` in config.json is both the replacement text and the upload target:
its path names what to write, as `/files/:model/:recordId/:field` on the service
subdomain (the older `/:serviceId/files/...` form is still accepted).

Saasufy's HTTP API rejects request bodies over 1,000,000 bytes with "Payload Too
Large", and base64 inflates the file by a third, so the source is whitespace-minified
until it fits. This is a transport limit on the API, not the field's `max`, which is
ten times larger. Measured by bisection against a request the schema rejects anyway:
999,983 bytes is parsed, 1,000,807 is refused.

Everything under assets/ (except assets/work-files/, which holds source material)
is uploaded first, one record per file in the `Asset` collection, and each
`assets/<path>` reference in the page is rewritten to that record's hosted URL,
`/files/Asset/:id/file`. The id is a UUIDv5 of the path, so a file keeps its URL
across deploys, and a `hash` field lets an unchanged file skip the upload. The
`Asset` model is declared in create-schema.py; the service must have been deployed
with it before the first run.

Each Saasufy OAuth environment is its own provider with its own Keycloak client
and registered redirect URIs, so `rewrites` in config.json must swap the
provider name and client ID over to production alongside the URL. Deploying with
the dev provider still in place authenticates the live app against the dev
client, whose only registered redirect URI is localhost.
"""
import base64, hashlib, json, mimetypes, os, re, sys, uuid
import urllib.error, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, 'config.json')))
KEY = open(os.path.join(ROOT, '.saasufy-api-key')).read().strip()

DEPLOY = CFG['deployURL'].rstrip('/')
DEV = CFG['devURL'].rstrip('/')
SOURCE = os.path.join(ROOT, CFG['source'])
DRY = '--dry-run' in sys.argv[1:]
BODY_LIMIT = 1000000  # measured; the API's cap on the whole request body

scheme, _, rest = DEPLOY.partition('://')
parts = rest.split('/')
# Both URL forms serve files: the subdomain one and the older path one, which puts the
# service id in the first path segment instead. Only the API prefix differs.
if scheme and len(parts) == 5 and parts[1] == 'files':
    api_root, (model, record, field) = parts[0], parts[2:]
elif scheme and len(parts) == 6 and parts[2] == 'files':
    api_root, (model, record, field) = '/'.join(parts[:2]), parts[3:]
else:
    raise SystemExit(
        f'deployURL must look like https://:serviceId.host/files/:model/:recordId/:field\n'
        f'  (or the older https://host/:serviceId/files/:model/:recordId/:field)\n'
        f'  got: {DEPLOY}')
API_ROOT = f'{scheme}://{api_root}/api'
API = f'{API_ROOT}/{model}/{record}'
FILES = DEPLOY.rsplit('/', 3)[0]  # .../files, shared by every hosted file

ASSETS = 'assets'
ASSET_MODEL, ASSET_FIELD = 'Asset', 'file'
ASSET_SKIP = {'work-files'}
ASSET_NS = uuid.UUID('b0a1c9d4-52e7-4f1b-8c36-7d0e9a4f2c15')


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', f'Bearer {KEY}')
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        return urllib.request.urlopen(req).read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f'{method} {url}\n  HTTP {e.code}: {e.read().decode()[:400]}')


# Longest pattern first: the bare origin would otherwise match inside the full URL.
dev_origin = DEV[:DEV.index('/', len(scheme) + 3)] if '/' in DEV[8:] else DEV
REWRITES = [(DEV, DEPLOY), (dev_origin + '/', DEPLOY), (dev_origin, DEPLOY)]
REWRITES += list(CFG.get('rewrites', {}).items())

source = open(SOURCE, encoding='utf-8').read()
out, total = source, 0
for old, new in REWRITES:
    n = out.count(old)
    out = out.replace(old, new)
    total += n
    print(f'  {n:>3} x  {old}\n         -> {new}')

print(f'\n{CFG["source"]}: {total} replacement(s)')
if not total:
    print('  nothing matched — check devURL and rewrites in config.json')
# Every dev token must be gone; anything left would reach production.
for token in [dev_origin] + list(CFG.get('rewrites', {})):
    lines = [i + 1 for i, l in enumerate(out.split('\n')) if token in l]
    if lines:
        print(f'  WARNING: "{token}" still present on line(s) {lines}')



def find_assets():
    """Paths under assets/, relative to it and /-separated. Skips work-files and dotfiles."""
    found = []
    for base, dirs, files in os.walk(os.path.join(ROOT, ASSETS)):
        top = base == os.path.join(ROOT, ASSETS)
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith('.') and not (top and d in ASSET_SKIP))
        for f in sorted(files):
            if not f.startswith('.'):
                full = os.path.join(base, f)
                found.append(os.path.relpath(full, os.path.join(ROOT, ASSETS)).replace(os.sep, '/'))
    return found


def asset_ref(rel):
    """`assets/<rel>` as a whole path, optionally written ./assets/ or /assets/."""
    return re.compile(r'(?<![\w./-])(?:\./|/)?' + re.escape(f'{ASSETS}/{rel}') + r'(?![\w.-])')


def upload_asset(rel, rid):
    """Returns what happened: 'unchanged', 'updated' or 'created'."""
    data = open(os.path.join(ROOT, ASSETS, rel), 'rb').read()
    digest = hashlib.sha256(data).hexdigest()
    kind = mimetypes.guess_type(rel)[0] or 'application/octet-stream'
    rec = {'name': rel, 'hash': digest,
           ASSET_FIELD: f'data:{kind};base64,' + base64.b64encode(data).decode()}
    size = len(json.dumps({'id': rid, **rec}).encode())
    if size > BODY_LIMIT:
        raise SystemExit(f'{ASSETS}/{rel}: {size} byte request is over the {BODY_LIMIT} '
                         f'byte limit — shrink the file (about 740 KB is the ceiling).')
    url = f'{API_ROOT}/{ASSET_MODEL}/{rid}'
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {KEY}')
    try:
        have = json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        if e.code != 404:
            hint = ('\n  The Asset model is missing: run ./create-schema.py, then deploy '
                    'the service.' if 'not defined on the schema' in detail else '')
            raise SystemExit(f'GET {url}\n  HTTP {e.code}: {detail}{hint}')
        have = None
    if have is None:
        call('POST', f'{API_ROOT}/{ASSET_MODEL}', {'id': rid, **rec})
        return 'created'
    if have.get('hash') == digest and have.get('name') == rel:
        return 'unchanged'
    call('PUT', url, rec)
    return 'updated'


# Assets go up before the page, so the page never points at a file that is not there.
assets = {rel: str(uuid.uuid5(ASSET_NS, rel)) for rel in find_assets()}
print(f'\n{ASSETS}/: {len(assets)} file(s)')
# Longest path first, so one asset's path cannot match inside another's.
for rel in sorted(assets, key=len, reverse=True):
    rid = assets[rel]
    url = f'{FILES}/{ASSET_MODEL}/{rid}/{ASSET_FIELD}'
    out, n = asset_ref(rel).subn(url, out)
    state = 'dry run' if DRY else upload_asset(rel, rid)
    print(f'  {n:>3} x  {ASSETS}/{rel}  ({state})\n         -> {url}')
    if not DRY and state != 'unchanged':
        got = urllib.request.urlopen(url).read()
        if got != open(os.path.join(ROOT, ASSETS, rel), 'rb').read():
            print(f'  WARNING: served copy of {ASSETS}/{rel} differs ({len(got)} bytes)')
# A reference to a file which was not uploaded would be a broken link in production.
for i, line in enumerate(out.split('\n')):
    for ref in re.findall(r'(?<![\w./-])(?:\./|/)?' + ASSETS + r'/[\w./-]+', line):
        print(f'  WARNING: "{ref}" on line {i + 1} has no uploaded asset behind it')

mime = mimetypes.guess_type(SOURCE)[0] or 'application/octet-stream'


def squeeze(text):
    """Leading indentation only. Newlines survive, so HTML still sees the
    whitespace it would have collapsed to a single space anyway."""
    return '\n'.join(l for l in (x.strip() for x in text.split('\n')) if l)


def decomment(text):
    return re.sub(r'<!--.*?-->', '', text, flags=re.S)


def body_for(text):
    uri = f'data:{mime};base64,' + base64.b64encode(text.encode()).decode()
    return json.dumps({field: uri}).encode()


STEPS = [('as authored', lambda t: t),
         ('indentation stripped', squeeze),
         ('indentation and comments stripped', lambda t: squeeze(decomment(t)))]

body = None
for label, fn in STEPS:
    candidate, size = fn(out), len(body_for(fn(out)))
    fits = size <= BODY_LIMIT
    print(f'  {label:<34} {len(candidate):>6} bytes -> {size:>6} body  '
          f'{"ok" if fits else "over limit"}')
    if fits:
        out, body = candidate, body_for(candidate)
        break

if body is None:
    raise SystemExit(f'\nCannot fit {CFG["source"]} under the {BODY_LIMIT} byte '
                     f'request limit even minified — split the file or upload it '
                     f'over the WebSocket API instead.')

print(f'  {BODY_LIMIT - len(body)} bytes of headroom')

if DRY:
    print('\n--dry-run: nothing uploaded.')
    raise SystemExit(0)

call('PUT', API, json.loads(body))
print(f'\nuploaded to {model}/{record}.{field}')

live = call('GET', DEPLOY).decode()
print('verified: served copy matches' if live == out
      else f'WARNING: served copy differs ({len(live)} bytes vs {len(out)})')
print(f'\n{DEPLOY}')
print('Keycloak must have this URL registered as a redirect URI, or login will '
      'fail after sign-in.')
