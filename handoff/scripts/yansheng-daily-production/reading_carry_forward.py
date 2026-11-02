"""Carry only demonstrably unchanged recorded readings into a new page snapshot."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from reading_audit import GROUPS, READ, SKIP, TOLERANCE, audit, read, sha, time

FILE_REF = re.compile(
    r'[A-Za-z]:[\\/][^\s()（）,，、；;:：`"\'|]+|[A-Za-z0-9_\-.]+\.(?:txt|json|jsonl|md|csv)\b')
CHUNK_REF = re.compile(r'chunk[-_ ]?(\d{1,2})')
DIGEST_HINT = re.compile(r'digest|摘要', re.I)
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024

def read_time_status(entry, sealed):
    """Before-seal, after-seal or unknown, using the original seal, never file mtime."""
    raw = entry.get('read_at')
    if raw in (None, ''):
        raw = entry.get('actual_read_at')
    if raw in (None, ''):
        return 'unknown'
    try:
        stamp = time(str(raw))
    except ValueError:
        return 'unknown'
    return 'before_seal' if stamp <= sealed + TOLERANCE else 'post_seal'

def evidence_text(entry):
    return ' '.join(str(entry.get(key) or '') for key in
                    ('reading_evidence', 'read_evidence', 'reading_note', 'evidence_path'))

def _resolve(ref, base_dirs):
    path = Path(ref)
    if path.is_absolute() and path.is_file():
        return path
    name = path.name
    for base in base_dirs:
        for candidate in (base / name, base / 'reading-chunks' / name):
            if candidate.is_file():
                return candidate
    return None

def _mentions(path, topic_id):
    try:
        with path.open('r', encoding='utf-8', errors='ignore') as handle:
            text = handle.read(MAX_EVIDENCE_BYTES)
    except OSError:
        return False
    return str(topic_id) in text

def evidence_is_traceable(entry, topic_id=None, base_dirs=()):
    """Locate the pointed-at file and confirm it actually mentions this post.

    A pass only means a retrievable artefact containing this topic id exists on disk.
    It does not prove the body was read, understood, or read before the seal.
    """
    text = evidence_text(entry)
    bases = [Path(base) for base in base_dirs]
    refs = list(FILE_REF.findall(text))
    refs += ['chunk-%02d.jsonl' % int(num) for num in CHUNK_REF.findall(text)]
    seen, found, missing, no_topic = [], None, [], []
    for ref in refs:
        name = Path(ref).name
        if name in seen:
            continue
        seen.append(name)
        path = _resolve(ref, bases)
        if path is None:
            missing.append(name)
            continue
        if topic_id is not None and not _mentions(path, topic_id):
            no_topic.append(str(path))
            continue
        found = path
        break
    strength = None
    if found is not None:
        strength = 'summary' if DIGEST_HINT.search(found.name) else 'body'
    return {'traceable': found is not None, 'strength': strength,
            'ref': str(found) if found is not None else None,
            'checked_refs': seen, 'missing_files': missing,
            'file_without_topic': no_topic,
            'note': 'A pass means the file exists and mentions this topic id; it is not proof of '
                    'reading, understanding or read-before-seal timing.'}

def collect(enum_path):
    enum=read(enum_path); start=time(enum['start']); cutoff=time(enum['cutoff'])
    if start>cutoff or {str(g['group']) for g in enum['groups']}!=GROUPS or len(enum['groups'])!=len(GROUPS):
        raise ValueError('invalid window or group set')
    captured=enum.get('captured_at') or enum.get('finished_at')
    if captured and time(captured)<cutoff: raise ValueError('window cutoff exceeds capture time')
    result={}
    for group in enum['groups']:
        gid=str(group['group']); pages=group['pages']; cursor=None; oldest=None; data=None
        if not pages: raise ValueError('group has no captured pages: '+gid)
        for index,page in enumerate(pages,1):
            if page['page']!=index: raise ValueError('page order mismatch')
            if page.get('fetched_at') and time(page['fetched_at'])<cutoff: raise ValueError('window cutoff exceeds page fetch time')
            path=Path(page['path']).resolve();raw=path.read_bytes()
            if sha(raw)!=page['sha256']: raise ValueError('page hash changed: '+str(path))
            data=json.loads(raw.decode('utf-8-sig'));rows=data['topics_brief']
            if not data.get('success') or (not rows and data.get('has_more')): raise ValueError('page request incomplete')
            if cursor and (not rows or rows[0]['create_time']!=cursor): raise ValueError('pagination boundary mismatch')
            cursor=data.get('next_end_time')
            for topic in rows:
                created=time(topic['create_time']);oldest=created if oldest is None else min(oldest,created)
                if not start<=created<=cutoff: continue
                ident=str(topic['topic_id']);body=(topic.get('content') or '').strip()
                item={'topic_id':ident,'group':gid,'created_at':topic['create_time'],
                      'source_page':str(path),'content_sha256':sha(body.encode('utf8')),
                      'modify_time':topic.get('modify_time') or '', 'type':topic.get('type')}
                if ident in result and any(result[ident][k]!=item[k] for k in ('group','content_sha256','modify_time')):
                    raise ValueError('conflicting versions of '+ident)
                result.setdefault(ident,item)
        if not group.get('reached_start') or (oldest is not None and oldest>start and data.get('has_more')):
            raise ValueError('window not reached for '+gid)
    return enum,result

def carry(new_enum_path, old_enum_path, old_ledger_path, old_coverage_path, manifest_path):
    paths=[Path(p).resolve() for p in (new_enum_path,old_enum_path,old_ledger_path,old_coverage_path,manifest_path)]
    new_enum, latest=collect(paths[0]); old_enum, original=collect(paths[1]); old=read(paths[2])
    manifest=read(paths[4])
    if not manifest.get('sealed_at'):
        raise ValueError('Previous manifest lacks sealed_at; cannot judge prior reading times')
    sealed=time(manifest['sealed_at'])
    prior_audit=audit(paths[1],paths[2],paths[3],sealed_at=manifest['sealed_at'])
    bound={str(Path(x['path']).resolve()):x['sha256'] for x in manifest.get('artifacts',[])}
    # Enumeration already pins every page hash; requiring a second manifest binding
    # for each page would reject a sound snapshot rather than protect it.
    declared={str(Path(page['path']).resolve()):page['sha256']
              for group in old_enum['groups'] for page in group['pages']}
    prior_ok=(prior_audit['status'] in ('COMPLETE_RECORDED','PARTIAL')
              and time(manifest['source_window_start'])==time(old_enum['start'])
              and time(manifest['source_cutoff'])==time(old_enum['cutoff']))
    for path in paths[1:4]:
        prior_ok=prior_ok and bound.get(str(path))==sha(path.read_bytes())
    for item in prior_audit.get('source_files',[]):
        prior_ok=prior_ok and (bound.get(item['path'])==item['sha256']
                               or declared.get(item['path'])==item['sha256'])
    bad_ids=set()
    for issue in prior_audit.get('issues',[]):
        for key in ('missing_ledger','outside_window','unsupported_or_missing_evidence',
                    'content_status_conflict_or_possible_truncation','remote_missing_ids_need_verification'):
            bad_ids.update(str(x) for x in issue.get(key,[]))
    records={str(x['topic_id']):x for x in old['entries']}
    ledger=[]; changed=[];new_ids=[]
    carried_verified=[];carried_unverified=[];post_seal=[];pending=[]
    pending_reason={};evidence_ref={}
    old_sha=sha(paths[2].read_bytes());new_cutoff=time(new_enum['cutoff'])
    for ident,item in latest.items():
        previous=records.get(ident);before=original.get(ident)
        carry_status=None
        if not previous or not before:
            new_ids.append(ident);state='enumerated_not_read'
        elif (prior_ok and ident not in bad_ids and previous.get('read_state') in READ | SKIP
              and item['type']=='talk' and item['group']==before['group']
              and item['content_sha256']==before['content_sha256']==previous.get('content_sha256')
              and item['modify_time']==before['modify_time']
              and (not item['modify_time'] or time(item['modify_time'])<=new_cutoff)):
            prior_state=previous['read_state']
            if prior_state in SKIP:
                state=prior_state;carry_status='carried_skip'
            else:
                stamp=read_time_status(previous,sealed)
                if stamp=='before_seal':
                    state=prior_state;carry_status='carried_verified';carried_verified.append(ident)
                elif stamp=='post_seal':
                    state='enumerated_not_read';carry_status='post_seal_read';post_seal.append(ident)
                elif (check:=evidence_is_traceable(previous,ident,[paths[2].parent]))['traceable'] \
                        and check['strength']=='body':
                    state=prior_state;carry_status='carried_time_unverified';carried_unverified.append(ident)
                    evidence_ref[ident]=check['ref']
                else:
                    state='enumerated_not_read';carry_status='pending_verification';pending.append(ident)
                    if DIGEST_HINT.search(evidence_text(previous)):
                        pending_reason[ident]='evidence_is_summary_only'
                    elif not check['checked_refs']:
                        pending_reason[ident]='no_file_or_time_pointer'
                    elif check['file_without_topic']:
                        pending_reason[ident]='file_exists_but_topic_absent'
                    else:
                        pending_reason[ident]='pointed_file_not_found'
        else:
            changed.append(ident);state='enumerated_not_read'
        row={**item,'read_state':state}
        if carry_status:
            row['carry_status']=carry_status
        if state in READ | SKIP and carry_status in ('carried_verified','carried_time_unverified','carried_skip'):
            row['reading_provenance']={'type':'unchanged_prior_record','previous_ledger':str(paths[2]),
                'previous_ledger_sha256':old_sha,'previous_topic_id':ident,'previous_source_page':previous['source_page'],
                'prior_sealed_at':manifest['sealed_at'],'read_time_status':read_time_status(previous,sealed)}
            if ident in evidence_ref:
                row['reading_provenance']['evidence_ref']=evidence_ref[ident]
                row['reading_provenance']['evidence_note']='File exists and mentions this topic id; not proof of reading.'
            if carry_status=='carried_time_unverified':
                row['read_time_verified']=False
        ledger.append(row)
    start=time(new_enum['start']);cutoff=time(new_enum['cutoff'])
    missing=sorted(ident for ident,x in original.items() if start<=time(x['created_at'])<=cutoff and ident not in latest)
    counts=Counter(x['read_state'] for x in ledger)
    status_counts=Counter(x['carry_status'] for x in ledger if x.get('carry_status'))
    summary={'schema':'yansheng-reading-carry-v1','prior_snapshot_integrity_verified':prior_ok,
             'prior_sealed_at':manifest['sealed_at'],'counts':dict(counts),
             'carry_status_counts':dict(status_counts),
             'read_time_verified_topic_ids':sorted(carried_verified),
             'read_time_unverified_topic_ids':sorted(carried_unverified),
             'post_seal_read_topic_ids':sorted(post_seal),
             'pending_verification_topic_ids':sorted(pending),
             'pending_verification_reasons':{k:sorted(v) for k,v in
                 {reason:sorted(i for i in pending if pending_reason.get(i)==reason)
                  for reason in set(pending_reason.values())}.items()},
             'carried_time_unverified_evidence_refs':evidence_ref,
             'new_topic_ids':sorted(new_ids),'changed_or_unverified_topic_ids':sorted(changed),
             'missing_prior_topic_ids':missing,'actual_reading_performed':False,
             'scope_note':'Only unchanged prior records are carried. Post-seal reads and records without a '
                          'verifiable time or a locatable evidence file mentioning the topic are listed for '
                          're-verification; they are not claims that the body was never read, nor that it '
                          'was read before the seal. Even a locatable file does not prove comprehension.'}
    coverage={'window_start':new_enum['start'],'cutoff':new_enum['cutoff'],
              'read_body_count':sum(counts[s] for s in READ),
              'read_body_count_time_verified':len(carried_verified),
              'read_body_count_time_unverified':len(carried_unverified),
              'post_seal_read_ids':sorted(post_seal),'pending_verification_ids':sorted(pending),
              'remote_missing_ids':missing,'note':'New/changed topics require actual reading; carried unverified '
              'records keep their time caveat and must not be reported as verified reading.'}
    return {'entries':ledger},coverage,summary

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('new-enumeration','previous-enumeration','previous-ledger','previous-coverage','previous-manifest','output-dir'):
        p.add_argument('--'+name,required=True,type=Path)
    a=p.parse_args();out=a.output_dir.resolve();out.mkdir(parents=True,exist_ok=True)
    try:
        ledger,coverage,summary=carry(a.new_enumeration,a.previous_enumeration,a.previous_ledger,a.previous_coverage,a.previous_manifest)
        files={'reading-ledger.json':ledger,'source-coverage.json':coverage,'carry-summary.json':summary}
        if any((out/name).exists() for name in files): raise ValueError('output exists; use a new revision')
        for name,value in files.items():
            with (out/name).open('x',encoding='utf8') as f: json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n')
        print(json.dumps(summary,ensure_ascii=False))
        return 0
    except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
        print(json.dumps({'status':'FAILED','error':str(exc)},ensure_ascii=False));return 2

if __name__=='__main__': raise SystemExit(main())
