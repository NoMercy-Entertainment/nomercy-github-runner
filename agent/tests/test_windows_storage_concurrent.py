from concurrent.futures import ThreadPoolExecutor
import threading
import pytest

from agent.runtimes.windows_storage import WindowsStorage

RID='3f2504e0-4f89-41d3-9a0c-0305e82c3301'


@pytest.mark.parametrize('failed', [False, True])
def test_concurrent_readers_share_full_verification_but_next_read_checks_again(failed):
    backend=WindowsStorage({},None,'powershell')
    entered=threading.Event()
    release=threading.Event()
    joined=threading.Event()
    calls=[]

    def check(action,rid):
        calls.append((action,rid))
        entered.set()
        assert release.wait(5)
        if failed:
            raise RuntimeError('volume identity changed')
        return {'runner_id':rid,'volume_guid':'owned'}

    backend._call=check
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(backend.verify,RID)
        assert entered.wait(5)
        original=backend._verifications[RID].result

        def waiting(*args,**kwargs):
            joined.set()
            return original(*args,**kwargs)

        backend._verifications[RID].result=waiting
        second=pool.submit(backend.verify,RID)
        assert joined.wait(5)
        release.set()
        if failed:
            for request in (first,second):
                with pytest.raises(RuntimeError,match='identity changed'):
                    request.result(5)
        else:
            one,two=first.result(5),second.result(5)
            assert one==two and one is not two
    assert len(calls)==1
    if failed:
        with pytest.raises(RuntimeError,match='identity changed'):
            backend.verify(RID)
    else:
        backend.verify(RID)
    assert len(calls)==2


def test_mutating_operations_keep_their_own_complete_checks():
    backend=WindowsStorage({},None,'powershell')
    calls=[]
    backend._call=lambda action,rid,limit=None: calls.append((action,rid)) or {}
    backend.mount(RID)
    backend.ensure(RID)
    backend.remove(RID)
    assert calls==[('mount',RID),('ensure',RID),('remove',RID)]


def test_different_runners_never_share_a_volume_proof():
    other='4f2504e0-4f89-41d3-9a0c-0305e82c3301'
    backend=WindowsStorage({},None,'powershell')
    both_entered=threading.Barrier(3)

    def check(action,rid):
        both_entered.wait(5)
        return {'runner_id':rid,'volume_guid':rid}

    backend._call=check
    with ThreadPoolExecutor(max_workers=2) as pool:
        one=pool.submit(backend.verify,RID)
        two=pool.submit(backend.verify,other)
        both_entered.wait(5)
        assert one.result(5)['volume_guid']==RID
        assert two.result(5)['volume_guid']==other
