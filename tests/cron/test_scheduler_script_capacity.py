"""Explicit script capacity must work beside a busy ordinary pool without adding model slots."""
from concurrent.futures import ThreadPoolExecutor
import threading

def test_opted_in_script_runs_while_ordinary_job_keeps_its_slot(monkeypatch, tmp_path):
    from cron import jobs, scheduler

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    release = threading.Event()
    occupied = threading.Event()
    control_ran = threading.Event()
    ordinary_followup = threading.Event()
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'control.py').write_text('print("bounded")\n')
    job = jobs.create_job(prompt='', schedule='1m', script='control.py', no_agent=True)
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'max_parallel_jobs': 1,
                        'control_script_job_ids': [job['id']]}})
    monkeypatch.setattr(scheduler, 'get_due_jobs', lambda: [job])
    monkeypatch.setattr(scheduler, 'run_one_job', lambda *a, **k: control_ran.set() or True)
    with scheduler._get_parallel_pool(1) as ordinary:
        def busy():
            occupied.set()
            release.wait(timeout=10)
        blocker = ordinary.submit(busy)
        try:
            assert occupied.wait(timeout=5)
            waiting = ordinary.submit(ordinary_followup.set)
            assert scheduler.tick(verbose=False, sync=False) == 1
            assert control_ran.wait(timeout=5)
            assert not blocker.done()
            assert not waiting.done()
            assert not ordinary_followup.is_set()
            # Ordinary/agent work can never select that extra worker, even with its ID listed.
            from cron import scheduler_script_capacity as capacity
            agent = dict(job, no_agent=False)
            assert capacity.select_pool(agent, ordinary, [job['id']])[0] is ordinary
            assert capacity.select_pool(job, ordinary, [])[0] is ordinary
        finally:
            release.set()
            import sys
            capacity = sys.modules.get('cron.scheduler_script_capacity')
            if capacity:
                capacity.discard(list(capacity._pools))
            scheduler._parallel_pools.clear()
            scheduler._parallel_pool_max_workers.clear()


def test_atomic_claim_refuses_job_changed_from_script_to_agent(monkeypatch, tmp_path):
    from cron import jobs, scheduler

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'control.py').write_text('print("bounded")\n')
    job = jobs.create_job(prompt='', schedule='1m', script='control.py', no_agent=True)
    stale_snapshot = dict(job, execution_id='owned-not-started', _script_only_capacity=True)
    jobs.update_job(job['id'], {'no_agent': False, 'prompt': 'changed to agent'})
    called = []
    monkeypatch.setattr(scheduler, 'run_one_job', lambda *a, **k: called.append(a) or True)
    scheduler._process_due_job(stale_snapshot, None, None, False)
    assert called == []
    assert not jobs.get_job(job['id']).get('fire_claim')


def test_script_capacity_is_profile_scoped_and_reclaimed(monkeypatch, tmp_path):
    from cron import scheduler_script_capacity as capacity
    from hermes_constants import hermes_home_key

    job = {'id': 'same-id', 'no_agent': True, 'script': 'control.py'}
    ordinary = object()
    first, second = tmp_path / 'first', tmp_path / 'second'
    monkeypatch.setenv('HERMES_HOME', str(first))
    one, _ = capacity.select_pool(job, ordinary, ['same-id'])
    monkeypatch.setenv('HERMES_HOME', str(second))
    two, _ = capacity.select_pool(job, ordinary, ['same-id'])
    try:
        assert one is not two
        capacity.discard([hermes_home_key(first)])
        assert hermes_home_key(first) not in capacity._pools
        assert capacity._pools[hermes_home_key(second)] is two
    finally:
        capacity.discard(list(capacity._pools))
