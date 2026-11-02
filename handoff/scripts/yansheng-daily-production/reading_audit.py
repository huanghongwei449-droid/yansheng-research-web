"""Reconcile recorded ZSXQ coverage; cannot prove that a person understood a post."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

CHINA = timezone(timedelta(hours=8))
GROUPS = {'15552822254242', '28512858211281'}
READ = {'full_body_read', 'full_body_read_limited_context'}
SKIP = {'link_only_skipped', 'file_only_skipped', 'nontext_skipped'}
PENDING = {'enumerated_not_read', 'truncated', 'unread', 'empty_content_pending'}
TOLERANCE = timedelta(seconds=60)

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def time(value):
    t = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return t.replace(tzinfo=CHINA) if t.tzinfo is None else t.astimezone(CHINA)

def temporal_integrity(entries, sealed_at=None, now=None):
    """Judge only whether recorded read times are possible and pre-seal.

    It never upgrades a read: an entry read after the seal stays post-seal even
    when the clock has since moved past that timestamp. Understanding is out of
    scope and is never asserted here.
    """
    current = now or datetime.now(CHINA)
    if current.tzinfo is None:
        raise ValueError('now requires timezone')
    current = current.astimezone(CHINA)
    seal = time(sealed_at) if isinstance(sealed_at, str) else (
        sealed_at.astimezone(CHINA) if sealed_at else None)
    result = {
        'schema': 'yansheng-reading-time-check-v1',
        'reference_sealed_at': seal.isoformat() if seal else None,
        'reference_now': current.isoformat(timespec='seconds'),
        'proves_understanding': False,
        'note': 'Only checks whether recorded times are possible and precede the seal; '
                'it cannot prove a body was read or understood.',
        'valid_before_seal': [], 'future_timestamp': [], 'post_seal_read': [],
        'time_unknown': [], 'skipped': 0, 'unread': 0,
    }
    if seal is None:
        result.update(decision='SEAL_REFERENCE_MISSING', checked_read=0,
                      reusable_before_seal=0, invalid_count=0, unknown_count=0)
        return result
    for entry in entries:
        state = entry.get('read_state', '')
        if state in SKIP:
            result['skipped'] += 1
            continue
        if state not in READ:
            result['unread'] += 1
            continue
        ident = str(entry.get('topic_id'))
        raw = entry.get('read_at')
        if raw in (None, ''):
            raw = entry.get('actual_read_at')
        if raw is None or str(raw).strip().lower() in {'', 'unknown', 'none'}:
            result['time_unknown'].append(ident)
            continue
        try:
            stamp = time(str(raw))
        except ValueError:
            result['time_unknown'].append(ident)
            continue
        if stamp > current + TOLERANCE:
            result['future_timestamp'].append(ident)
        elif stamp > seal + TOLERANCE:
            result['post_seal_read'].append(ident)
        else:
            result['valid_before_seal'].append(ident)
    invalid = len(result['future_timestamp']) + len(result['post_seal_read'])
    result.update(
        checked_read=sum(len(result[k]) for k in
                         ('valid_before_seal', 'future_timestamp', 'post_seal_read', 'time_unknown')),
        reusable_before_seal=len(result['valid_before_seal']),
        invalid_count=invalid, unknown_count=len(result['time_unknown']),
        decision=('INVALID_READ_TIME' if invalid else
                  'TIME_UNVERIFIED' if result['time_unknown'] else 'TIMES_VALID'))
    return result

def audit(enumeration_path, ledger_path, coverage_path, sealed_at=None, now=None):
    paths = [Path(p).resolve() for p in (enumeration_path, ledger_path, coverage_path)]
    result = {'schema':'yansheng-zsxq-reading-audit-v1', 'status':'UNKNOWN',
              'semantic_reading_verified':False, 'reading_quality_verified':False,
              'read_time_verified':False, 'remote_edit_recheck_required':True,
              'temporal_integrity':None, 'issues':[], 'input_files':[
                  {'path':str(p), 'sha256':sha(p.read_bytes()) if p.is_file() else None} for p in paths]}
    try:
        enum, ledger, coverage = map(read, paths)
        entries = ledger.get('entries') if isinstance(ledger.get('entries'), list) else None
        temporal = temporal_integrity(entries, sealed_at=sealed_at, now=now) if entries is not None and sealed_at else None
        invalid_time = set(temporal['future_timestamp'] + temporal['post_seal_read']) if temporal else set()
        # A read recorded after the seal or in the future cannot stand as pre-seal reading,
        # even if the wall clock has since passed that moment.
        start, cutoff = time(enum['start']), time(enum['cutoff'])
        if cutoff < start or time(coverage['window_start']) != start or time(coverage['cutoff']) != cutoff:
            raise ValueError('window mismatch')
        captured=enum.get('captured_at') or enum.get('finished_at')
        if captured and time(captured) < cutoff:
            raise ValueError('window cutoff exceeds capture time')
        groups = enum['groups']
        if {str(g['group']) for g in groups} != GROUPS or len(groups) != len(GROUPS):
            raise ValueError('two expected groups required')
        topics = {}; topic_modify = {}; topic_type = {}; source_paths = {}; source_files = []; incomplete_groups = []
        for group in groups:
            gid=str(group['group']); pages=group['pages']; cursor=None; oldest=None
            if not pages: raise ValueError('missing group page: '+gid)
            for index, page in enumerate(pages, 1):
                if page['page'] != index: raise ValueError('page sequence mismatch: '+gid)
                if page.get('fetched_at') and time(page['fetched_at']) < cutoff:
                    raise ValueError('window cutoff exceeds page fetch time: '+gid)
                p=Path(page['path']).resolve(); raw=p.read_bytes(); calculated=sha(raw)
                if calculated != page['sha256']: raise ValueError('page hash mismatch: '+str(p))
                source_files.append({'path':str(p),'sha256':calculated})
                data=json.loads(raw.decode('utf-8-sig')); rows=data['topics_brief']
                if not data.get('success'): raise ValueError('page request failed: '+str(p))
                if not rows:
                    if data.get('has_more'): raise ValueError('empty page with more pages: '+str(p))
                    cursor=None
                    continue
                if cursor and rows[0]['create_time'] != cursor: raise ValueError('pagination boundary mismatch: '+gid)
                cursor=data.get('next_end_time')
                for x in rows:
                    t=time(x['create_time']); oldest=t if oldest is None else min(oldest,t)
                    if not start <= t <= cutoff: continue
                    ident=str(x['topic_id']); text=(x.get('content') or '').strip()
                    if ident in topics and topics[ident] != (gid,text):
                        raise ValueError('duplicate topic has different body: '+ident)
                    topics[ident]=(gid,text); source_paths.setdefault(ident,set()).add(str(p))
                    topic_modify[ident]=x.get('modify_time')
                    topic_type[ident]=x.get('type')
            if not group.get('reached_start') or (oldest is not None and oldest > start and data.get('has_more')):
                incomplete_groups.append(gid)
        entries=ledger['entries']; ids=[str(x['topic_id']) for x in entries]
        if len(set(ids))!=len(ids): raise ValueError('duplicate ledger topic')
        missing=sorted(set(topics)-set(ids)); extra=sorted(set(ids)-set(topics))
        if missing or extra: result['issues'].append({'missing_ledger':missing,'outside_window':extra})
        counts=Counter(); unknown=[]; suspicious=[]
        for entry in entries:
            ident=str(entry['topic_id'])
            if ident not in topics: continue
            gid, body=topics[ident]; state=entry.get('read_state','')
            if str(entry.get('group'))!=gid: raise ValueError('group mismatch: '+ident)
            # Q&A and other topic types can contain nested question/answer text;
            # this reader cannot infer their full coverage from talk.content.
            if topic_type.get(ident) != 'talk':
                unknown.append(ident); continue
            if topic_modify.get(ident) and time(topic_modify[ident]) > cutoff:
                unknown.append(ident); continue
            source=Path(entry.get('source_page','')).resolve()
            if str(source) not in source_paths[ident]:
                # A detail response may supply text truncated in the list.
                detail_raw=source.read_bytes(); detail=json.loads(detail_raw.decode('utf-8-sig'))
                topic=detail.get('topic') or {}
                if not detail.get('success') or str(topic.get('topic_id')) != ident:
                    unknown.append(ident); continue
                detail_body=(topic.get('content') or '').strip()
                changed=topic.get('modify_time')
                if detail_body != body:
                    fetched=entry.get('detail_fetched_at')
                    prefix=re.sub(r'(?:\.\.\.|…|展开全文)\s*$', '', body).strip()
                    if changed and time(changed) > cutoff:
                        unknown.append(ident); continue
                    if not changed and not (fetched and time(fetched) <= cutoff and prefix
                                            and detail_body.startswith(prefix)):
                        unknown.append(ident); continue
                body=detail_body
                source_files.append({'path':str(source),'sha256':sha(detail_raw)})
            if entry.get('content_sha256') != sha(body.encode('utf-8')):
                unknown.append(ident); continue
            if state in SKIP:
                visible=re.sub(r'<e\b[^>]*?/?>','',body).strip()
                if visible and visible not in {'「文件」','「图片」','「音频」','「视频」'}:
                    suspicious.append(ident); continue
            visible=re.sub(r'<e\b[^>]*?/?>','',body).strip()
            if state in READ and (not visible or visible in {'「文件」','「图片」','「音频」','「视频」'}
                                  or re.search(r'(?:\.\.\.|…|展开全文)\s*$',body)):
                suspicious.append(ident); continue
            if state not in READ | SKIP | PENDING:
                unknown.append(ident); continue
            if ident in invalid_time:
                # Counted separately as an invalid read time, never as a read.
                continue
            counts[state]+=1
        if unknown: result['issues'].append({'unsupported_or_missing_evidence':unknown})
        if suspicious: result['issues'].append({'content_status_conflict_or_possible_truncation':suspicious})
        if incomplete_groups: result['issues'].append({'incomplete_pagination_groups':incomplete_groups})
        if coverage.get('remote_missing_ids'):
            result['issues'].append({'remote_missing_ids_need_verification':coverage['remote_missing_ids']})
        declared=coverage.get('read_body_count')
        counted=sum(counts[s] for s in READ)
        if declared is not None and declared!=counted:
            result['issues'].append({'declared_read_count':declared,'reconciled_read_count':counted})
        pending=sum(counts[s] for s in PENDING)
        result.update(window_start=start.isoformat(),window_cutoff=cutoff.isoformat(),
                      enumerated=len(topics),groups=sorted(GROUPS),states=dict(counts),
                      source_files=source_files)
        if temporal:
            result['temporal_integrity']=temporal
            result['read_time_verified']=temporal['decision']=='TIMES_VALID'
            if temporal['future_timestamp']:
                result['issues'].append({'future_read_time':temporal['future_timestamp'],
                    'note':'Read times after the current execution are impossible; never count as read.'})
            if temporal['post_seal_read']:
                result['issues'].append({'post_seal_read':temporal['post_seal_read'],
                    'note':'Read after this package was sealed; must enter a new revision as an increment, not as pre-seal reading.'})
            if temporal['time_unknown']:
                # Unknown time is honest but unverified: it neither proves nor disproves reading.
                result['temporal_caveats']=[{'read_time_unknown_count':len(temporal['time_unknown']),
                    'note':'Recorded as unknown rather than fabricated; treated as unverified time, not as unread.'}]
        result['status']='PARTIAL' if result['issues'] or pending else 'COMPLETE_RECORDED'
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        result['issues'].append({'error':str(exc)})
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--enumeration',type=Path,required=True)
    p.add_argument('--ledger',type=Path,required=True)
    p.add_argument('--coverage',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--sealed-at',help='Original seal time; read times after it are post-seal reads')
    p.add_argument('--now',help='Execution time override (ISO 8601, timezone required)')
    args=p.parse_args()
    now=time(args.now) if args.now else None
    result=audit(args.enumeration,args.ledger,args.coverage,sealed_at=args.sealed_at,now=now)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:result.get(k) for k in ('status','enumerated','states','issues',
                                                 'semantic_reading_verified','read_time_verified')},ensure_ascii=False))
    return 0 if result['status']=='COMPLETE_RECORDED' else 2

if __name__=='__main__':
    raise SystemExit(main())
