"""Build/load a review-only diff in memory. Never modify backend or configure it."""
import difflib
import hashlib
from pathlib import Path
import sys
import types

REPO = Path(__file__).resolve().parents[2]
TARGETS = (
    'backend/paid_source_access.py',
    'experiments/free_search/owner_access.py',
    'experiments/free_search/owner_delivery.py',
)


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError('proposal_base_changed')
    return text.replace(before, after, 1)


def proposal():
    originals = {path: (REPO/path).read_text(encoding='utf-8') for path in TARGETS}
    proposed = dict(originals)
    path = TARGETS[0]
    text = replace_once(originals[path],
        'EXCLUSIONS = "autodeal_paid_sources_excluded_accounts"',
        'EXCLUSIONS = "autodeal_paid_sources_excluded_accounts"\n'
        '# Explicit engine option only; no environment/default activation.\n'
        'PAID_JOB_EXCLUSIONS = "autodeal_paid_job_excluded_accounts"')
    text = replace_once(text,
        'def exclusions(bind):\n'
        '    return bind.get_execution_options().get(EXCLUSIONS, source_excluded_user_ids())',
        'def entitlement_exclusions(bind):\n'
        '    return bind.get_execution_options().get(EXCLUSIONS, source_excluded_user_ids())\n'
        '\n\n'
        'def exclusions(bind):\n'
        '    return tuple(sorted(set(entitlement_exclusions(bind)) | set(\n'
        '        bind.get_execution_options().get(PAID_JOB_EXCLUSIONS, ()))))\n'
        '\n\n'
        'def confirmed_allowed(db, uid, now=None):\n'
        '    """Confirmed entitlement only; never authorizes paid provider work.\n'
        '\n'
        '    Retain account exclusions, purchase and entitlement expiry. A job\n'
        '    exclusion only removes paid work; it does not revoke the purchase.\n'
        '    Free caller still must verify owner identity, ready, filters and epoch.\n'
        '    """\n'
        '    if not strict(db.get_bind()):\n'
        '        return False\n'
        '    return bool(db.scalar(select(confirmed_clause(\n'
        '        uid, now, excluded=entitlement_exclusions(db.get_bind())))))')
    proposed[path] = text
    for path in TARGETS[1:]:
        proposed[path] = replace_once(originals[path],
            'paid_source_access.allowed(db,',
            'paid_source_access.confirmed_allowed(db,')
    def diff(before, after):
        pieces = []
        for path in TARGETS:
            crlf = b'\r\n' in (REPO/path).read_bytes()
            for line in difflib.unified_diff(
                    before[path].splitlines(True), after[path].splitlines(True),
                    fromfile='a/'+path, tofile='b/'+path):
                # Hunk data must match each target's actual line endings.
                # Windows tracked files and LF-only untracked experiments coexist.
                if crlf and line[:1] in (' ', '+', '-') and not line.startswith(('+++', '---')):
                    line = line.removesuffix('\n')+'\r\n'
                pieces.append(line)
        return ''.join(pieces)
    patch, reverse = diff(originals, proposed), diff(proposed, originals)
    return originals, proposed, patch, reverse


def load_review_modules():
    """Compile exact proposed sources under isolated module names for tests."""
    originals, proposed, patch, reverse = proposal()
    modules = []
    for path, name in zip(TARGETS, (
            'backend._owner_review_paid_access',
            'experiments.free_search._owner_review_access',
            'experiments.free_search._owner_review_delivery')):
        module = types.ModuleType(name)
        module.__file__ = str(REPO/path)
        module.__package__ = name.rpartition('.')[0]
        sys.modules[name] = module
        exec(compile(proposed[path], str(REPO/path)+' [review only]', 'exec'), module.__dict__)
        modules.append(module)
    modules[1].paid_source_access = modules[0]
    modules[2].paid_source_access = modules[0]
    return tuple(modules)


def write_artifacts(directory):
    """Called explicitly under night lock; writes only the review directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    originals, proposed, patch, reverse = proposal()
    import json
    (directory/'owner-access.patch').write_bytes(patch.encode('utf-8'))
    (directory/'owner-access.reverse.patch').write_bytes(reverse.encode('utf-8'))
    manifest = {
        'applied': False, 'activation_configured': False,
        'base_sha256': {p: hashlib.sha256(t.encode()).hexdigest() for p,t in originals.items()},
        'proposed_sha256': {p: hashlib.sha256(t.encode()).hexdigest() for p,t in proposed.items()},
        'limitations': ['paid UI gates still share allowed()', 'no in-flight paid-job drain',
                        'no deployment wiring', 'collector/peer readiness still missing']}
    (directory/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest
