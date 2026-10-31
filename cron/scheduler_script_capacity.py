"""Opt-in capacity for existing no-agent control jobs, under native tick ownership.

Ordinary cron keeps its configured total limit. An operator may reserve one
additional script-only worker per profile for exact existing job IDs. This is
not a ticker and grants no model worker or new job/admission authority.
"""
import atexit
from concurrent.futures import ThreadPoolExecutor

from hermes_constants import get_hermes_home, hermes_home_key

_pools: dict[str, ThreadPoolExecutor] = {}


def select_pool(job, ordinary_pool, control_ids):
    if (not isinstance(control_ids, list) or not all(isinstance(x, str) and x for x in control_ids)
            or job.get('id') not in control_ids or job.get('no_agent') is not True
            or not job.get('script')):
        return ordinary_pool, job
    key = hermes_home_key(get_hermes_home())
    if key not in _pools:
        _pools[key] = ThreadPoolExecutor(max_workers=1, thread_name_prefix='cron-control-script')
    return _pools[key], dict(job, _script_only_capacity=True)


def fire_claim_options(job):
    # Revalidated inside the persisted job's atomic claim, not a stale due snapshot.
    return {'return_job': True, **({'script_only': True} if job.get('_script_only_capacity') else {})}


def discard(home_keys):
    for key in home_keys:
        pool = _pools.pop(key, None)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=False)


atexit.register(lambda: discard(list(_pools)))
