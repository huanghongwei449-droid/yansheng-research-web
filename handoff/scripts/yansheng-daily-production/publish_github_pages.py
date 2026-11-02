"""Publish only reviewed public static bytes; never exports research or reads tokens."""
import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(args, cwd=None, allow_failure=False):
    p = subprocess.run(args, cwd=cwd, capture_output=True, encoding='utf-8', errors='replace')
    require(allow_failure or p.returncode == 0, p.stderr.strip() or p.stdout.strip())
    return p.stdout.strip()


def api(path):
    return json.loads(command(['gh', 'api', path]))


def fetch(url):
    with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0 Yansheng-publication-verification',
                                       'Cache-Control': 'no-cache'}), timeout=30) as response:
        return response.read()


def validate(source, state_path):
    workspace = Path(__file__).resolve().parents[4]
    module_path = workspace / '.agents/skills/yansheng-daily-production/scripts/workflow.py'
    spec = importlib.util.spec_from_file_location('daily_workflow', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = module.load_state(state_path)
    require(len(state['stages']) >= 6, 'review and build must complete before publishing')
    report = module.review_check(state)
    build = module.read(module.artifact(state, 'build', 'web-build-receipt.json'))
    module.artifact(state, 'build', 'page-review.md')
    require(build['date'] == state['date'] and build['session'] == state['session']
            and build['body_sha256'] == digest(report), 'build differs from reviewed report')
    publication = json.loads((source / 'publication.json').read_text(encoding='utf-8'))
    identity = ':'.join([state['date'], state['session'], digest(report)])
    require(publication['latest'][state['session']] == identity, 'source not latest reviewed report')
    release = publication['release_id']
    require(re.fullmatch(r'[a-f0-9]{24}', release), 'invalid release')
    bundle_path = source / publication['bundle']['path']
    require(bundle_path.parent == source and digest(bundle_path) == publication['bundle']['sha256'], 'bundle mismatch')
    bundle = json.loads(bundle_path.read_text(encoding='utf-8'))
    require(bundle['release_id'] == release and bundle['schema'] == 'yansheng-workbench-v1', 'wrong bundle')
    required = {'index.html', 'today.html', 'companies.html', 'company.html', 'directions.html',
                'opportunities.html', 'publication.json', 'workbench.js'}
    records = []
    for p in sorted(source.iterdir()):
        require(p.is_file() and not p.is_symlink() and re.fullmatch(r'[a-zA-Z0-9_.-]+', p.name), 'unexpected public path')
        require(not p.name.startswith('.') and p.suffix in {'.html', '.json', '.js', '.css', '.png', '.jpg', '.jpeg', '.svg', '.ico', '.webp', '.woff', '.woff2'}, 'non-public file')
        require(not any(x in p.name.lower() for x in ('secret', 'credential', '.env')), 'credential-like file')
        records.append({'path': p.name, 'sha256': digest(p), 'bytes': p.stat().st_size})
    require(required <= {r['path'] for r in records}, 'public pages missing')
    return state, publication, records, digest(report)


def publish(args):
    source = args.source.resolve()
    checkout = args.checkout.resolve()
    state, publication, files, report_sha = validate(source, args.workflow)
    require(re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', args.repo), 'invalid repository')
    remote = f'https://github.com/{args.repo}.git'
    require(command(['git', 'remote', 'get-url', 'origin'], checkout) == remote, 'unexpected remote')
    require(api(f'repos/{args.repo}')['private'] is False, 'dedicated public repository required')
    require(command(['git', 'branch', '--show-current'], checkout) == 'main', 'main branch required')
    require(not command(['git', 'status', '--porcelain'], checkout), 'checkout contains uncommitted changes; inspect first')
    if not args.publish:
        return {'status': 'VALIDATED_NOT_DEPLOYED', 'report_sha256': report_sha, 'files': len(files)}
    require(not args.receipt.exists(), 'receipt already exists; choose a separate verification receipt')
    # Windows' global autocrlf must not change immutable bundle bytes or its SHA.
    command(['git', 'config', 'core.autocrlf', 'false'], checkout)
    # No deletes. Prior reports and releases are immutable history.
    names = []
    for item in files:
        target = checkout / item['path']
        if target.exists() and item['path'].startswith(('release-', 'report-')):
            require(digest(target) == item['sha256'], 'immutable public history changed: ' + item['path'])
    # Check every immutable file before copying any mutable current page.
    for item in files:
        target = checkout / item['path']
        shutil.copyfile(source / item['path'], target)
        names.append(item['path'])
    (checkout / '.nojekyll').touch()
    (checkout / '.gitattributes').write_bytes(b'* -text\n')
    command(['git', 'add', '--', *names, '.nojekyll', '.gitattributes'], checkout)
    command(['git', 'add', '--renormalize', '--', *names], checkout)
    changed = bool(command(['git', 'diff', '--cached', '--name-only'], checkout))
    if changed:
        command(['git', 'commit', '-m', f"Publish {state['date']} {state['session']} {publication['release_id']}"], checkout)
    commit = command(['git', 'rev-parse', 'HEAD'], checkout)
    command(['git', 'push', 'origin', 'main'], checkout)
    deadline = time.monotonic() + args.wait_seconds
    build = None
    while time.monotonic() < deadline:
        try:
            build = api(f'repos/{args.repo}/pages/builds/latest')
        except ValueError:
            build = None
        if build and build.get('commit') == commit:
            require(build['status'] != 'errored', 'Pages build failed: ' + str(build.get('error')))
            if build['status'] == 'built':
                break
        time.sleep(5)
    require(build and build.get('commit') == commit and build.get('status') == 'built',
            'current Pages build still pending; rerun with same checkout and report')
    pages = api(f'repos/{args.repo}/pages')
    base = pages['html_url'].rstrip('/') + '/'
    # Verify public bytes independently of GitHub's build status and authentication.
    checks = []
    verify = {'publication.json', publication['bundle']['path'], 'workbench.js',
              'today.html', 'companies.html', 'company.html', 'opportunities.html', 'directions.html'}
    for item in files:
        if item['path'] not in verify:
            continue
        actual = None
        for attempt in range(3):
            try:
                raw = fetch(base + item['path'] + '?verify=' + commit[:12])
                actual = hashlib.sha256(raw).hexdigest()
                if actual == item['sha256']:
                    break
            except Exception:
                pass
            time.sleep(3)
        require(actual == item['sha256'], 'anonymous readback differs: ' + item['path'])
        checks.append({'path': item['path'], 'sha256': actual})
    receipt = {'schema': 'yansheng-github-pages-deployment-v1', 'status': 'succeeded',
               'host': 'github-pages', 'research_date': state['date'], 'session': state['session'],
               'report_sha256': report_sha, 'release_id': publication['release_id'],
               'project_id': args.repo, 'version_id': commit, 'deployment_id': build['url'],
               'url': base, 'files': len(files), 'checks': checks,
               'verified_at': datetime.now(timezone.utc).isoformat(),
               'target_network_verified': False, 'new_commit': changed}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'status': receipt['status'], 'url': base, 'commit': commit, 'report_sha256': report_sha,
            'new_commit': changed, 'target_network_verified': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True, type=Path)
    p.add_argument('--workflow', required=True, type=Path)
    p.add_argument('--checkout', required=True, type=Path)
    p.add_argument('--repo', required=True)
    p.add_argument('--receipt', required=True, type=Path)
    p.add_argument('--wait-seconds', type=int, default=180)
    p.add_argument('--publish', action='store_true')
    print(json.dumps(publish(p.parse_args()), ensure_ascii=False))
