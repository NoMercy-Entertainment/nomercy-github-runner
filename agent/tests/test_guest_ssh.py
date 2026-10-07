"""The macOS guest reached over SSH, which is what makes the appliance a
worker without touching it.

The appliance is a QEMU guest whose only way in is the SSH port its host
forwards. The agent therefore runs on the appliance host - the worker kind
the schema already names `macos-appliance-host` - and gives the runtime a
`run` and an `fs` that act inside the guest. Everything the runtime asks of
a local disk becomes one command over one multiplexed connection.

What these tests hold it to: the guest's own quoting (a path with a space
must not become two arguments), the BSD tools macOS actually has, a password
that never reaches a command line, and `write_text`'s promise that a private
file is never briefly readable.
"""
import pytest

from agent.runtimes.guest_ssh import GuestExec, GuestFs


class FakeRun:
    """Stands in for subprocess: records calls, answers from a script."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = dict(answers or {})
        self.default = (0, "", "")

    def __call__(self, argv, input=None, timeout=None, env=None):
        self.calls.append({"argv": list(argv), "input": input,
                           "timeout": timeout, "env": dict(env or {})})
        remote = argv[-1]
        for fragment, answer in self.answers.items():
            if fragment in remote:
                return answer
        return self.default

    @property
    def remote(self):
        """The remote command of the last call."""
        return self.calls[-1]["argv"][-1]


GUEST = {"host": "127.0.0.1", "port": 50922, "user": "user"}


@pytest.fixture
def run():
    return FakeRun()


@pytest.fixture
def exec_(run):
    return GuestExec(**GUEST, ssh="/usr/bin/ssh", runner=run)


@pytest.fixture
def fs(exec_):
    return GuestFs(exec_)


class TestReachingTheGuest:
    def test_it_ssh_es_to_the_guests_forwarded_port(self, exec_, run):
        exec_(["/usr/bin/true"])
        argv = run.calls[-1]["argv"]
        assert argv[0] == "/usr/bin/ssh"
        assert "-p" in argv and "50922" in argv
        assert "user@127.0.0.1" in argv

    def test_it_multiplexes_so_a_sweep_is_one_connection(self, exec_, run):
        exec_(["/usr/bin/true"])
        options = " ".join(run.calls[-1]["argv"])
        assert "ControlMaster=auto" in options
        assert "ControlPath=" in options
        assert "ControlPersist=" in options

    def test_forwarded_guests_and_users_have_separate_connections(self):
        mac = GuestExec("127.0.0.1", "user", port=50922)
        arm = GuestExec("127.0.0.1", "user", port=52222)
        other_user = GuestExec("127.0.0.1", "admin", port=50922)
        assert len({mac._control, arm._control, other_user._control}) == 3
        assert mac._control == GuestExec("127.0.0.1", "user", port=50922)._control

    def test_refusing_master_is_retired_without_replaying_the_command(self, run):
        run.answers = {"/usr/bin/true": (0, "done", "mux_client_request_session: session request failed: Session open refused by peer")}
        got = GuestExec(**GUEST, runner=run)(["/usr/bin/true"])
        assert got[:2] == (True, "done")
        assert len(run.calls) == 2
        assert run.calls[1]["argv"][-3:-1] == ["-O", "stop"]
        assert "/usr/bin/true" not in run.calls[1]["argv"]

    def test_it_never_asks_a_human_anything(self, exec_, run):
        exec_(["/usr/bin/true"])
        options = " ".join(run.calls[-1]["argv"])
        assert "BatchMode=yes" in options or "PasswordAuthentication" in options

    def test_an_argument_with_a_space_stays_one_argument(self, exec_, run):
        exec_(["/bin/echo", "/Users/runner/two words/file"])
        assert "'/Users/runner/two words/file'" in run.remote

    def test_the_exit_status_is_the_answer(self, run):
        run.default = (3, "out", "err")
        ok, out, err = GuestExec(**GUEST, runner=run)(["/usr/bin/false"])
        assert (ok, out, err) == (False, "out", "err")

    def test_input_reaches_the_guests_standard_input(self, exec_, run):
        exec_(["/usr/bin/tee"], input='{"plan": true}')
        assert run.calls[-1]["input"] == '{"plan": true}'

    def test_a_timeout_is_passed_on_rather_than_waited_out(self, exec_, run):
        exec_(["/usr/bin/true"], timeout=7)
        assert run.calls[-1]["timeout"] == 7

    def test_a_dead_connection_is_a_failure_not_an_exception(self, run):
        def explode(argv, **kw):
            raise OSError("host is down")

        ok, out, err = GuestExec(**GUEST, runner=explode)(["/usr/bin/true"])
        assert ok is False
        assert "host is down" in err


class TestTheSecret:
    """A password is a credential: it goes through the environment, never a
    command line every process on the host can read."""

    def test_a_password_goes_through_sshpass_and_the_environment(self, run):
        exec_ = GuestExec(**GUEST, password="alpine",
                          sshpass="/usr/bin/sshpass", runner=run)
        exec_(["/usr/bin/true"])
        argv = run.calls[-1]["argv"]
        assert argv[0] == "/usr/bin/sshpass"
        assert "-e" in argv
        assert run.calls[-1]["env"].get("SSHPASS") == "alpine"
        assert "alpine" not in " ".join(argv)

    def test_a_key_is_used_alone_so_an_agent_cannot_offer_another(self, run):
        exec_ = GuestExec(**GUEST, key="/home/runner/.ssh/guest", runner=run)
        exec_(["/usr/bin/true"])
        options = " ".join(run.calls[-1]["argv"])
        assert "IdentitiesOnly=yes" in options
        assert "/home/runner/.ssh/guest" in options

    def test_repr_does_not_carry_the_password(self, run):
        exec_ = GuestExec(**GUEST, password="alpine", runner=run)
        assert "alpine" not in repr(exec_)


class TestTheDiskInsideTheGuest:
    def test_exists_is_the_guests_answer(self, fs, run):
        run.answers = {"test -e": (0, "present", "")}
        assert fs.exists("/Users/runner/runners") is True
        run.answers = {"test -e": (0, "absent", "")}
        assert fs.exists("/Users/runner/runners") is False

    def test_makedirs_does_not_mind_an_existing_directory(self, fs, run):
        fs.makedirs("/Users/runner/runners/a/b")
        assert "mkdir -p" in run.remote

    def test_chmod_speaks_octal_as_a_shell_does(self, fs, run):
        fs.chmod("/Users/runner/runners/a", 0o700)
        assert "chmod 700" in run.remote

    def test_listdir_is_sorted_and_hidden_entries_count(self, fs, run):
        run.answers = {"ls -1A": (0, "b\n.a\nc\n", "")}
        assert fs.listdir("/Users/runner") == [".a", "b", "c"]

    def test_listdir_of_something_that_is_not_a_directory_is_empty(self, fs,
                                                                   run):
        run.answers = {"ls -1A": (0, "", "")}
        assert fs.listdir("/Users/runner/file") == []

    @pytest.mark.parametrize("method", ["exists", "listdir"])
    def test_an_unreachable_guest_is_not_an_absent_file_or_empty_fleet(self, fs, run, method):
        run.answers = {"test -e": (255, "", "connection refused"),
                       "ls -1A": (255, "", "connection refused")}
        with pytest.raises(OSError, match="guest"):
            getattr(fs, method)("/Users/runner/runners")

    def test_copytree_copies_the_contents_into_an_existing_destination(
            self, fs, run):
        fs.copytree("/Users/runner/templates/t", "/Users/runner/r/reg")
        assert "cp -R" in run.remote
        assert "/Users/runner/templates/t/." in run.remote

    def test_read_text_is_what_the_guest_holds(self, fs, run):
        run.answers = {"cat ": (0, "hello", "")}
        assert fs.read_text("/Users/runner/f") == "hello"

    def test_read_text_of_something_unreadable_says_so(self, fs, run):
        run.answers = {"cat ": (1, "", "No such file or directory")}
        with pytest.raises(OSError):
            fs.read_text("/Users/runner/missing")

    def test_write_text_is_atomic_and_private_before_it_has_content(self, fs,
                                                                    run):
        fs.write_text("/Users/runner/r/reg/.env", "TOKEN=x", mode=0o600)
        remote = run.remote
        assert remote.index("chmod 600") < remote.index("cat >")
        assert "mv " in remote
        assert remote.index("cat >") < remote.index("mv ")

    def test_write_text_sends_the_content_as_input_not_as_an_argument(
            self, fs, run):
        fs.write_text("/Users/runner/r/reg/.env", "TOKEN=secret-value")
        assert run.calls[-1]["input"] == "TOKEN=secret-value"
        assert "secret-value" not in run.remote

    def test_write_text_that_fails_raises_rather_than_reporting_success(
            self, fs, run):
        run.answers = {"cat >": (1, "", "Read-only file system")}
        with pytest.raises(OSError):
            fs.write_text("/Users/runner/f", "x")

    def test_tail_reads_only_the_end(self, fs, run):
        run.answers = {"tail -c": (0, "last lines", "")}
        assert fs.tail("/Users/runner/r/logs/run.log", 4096) == "last lines"
        assert "tail -c 4096" in run.remote

    def test_tail_of_a_missing_log_is_empty_not_an_error(self, fs, run):
        run.answers = {"tail -c": (1, "", "No such file")}
        assert fs.tail("/Users/runner/r/logs/run.log", 4096) == ""

    def test_mtime_uses_the_stat_macos_has(self, fs, run):
        run.answers = {"stat": (0, "1758326400\n", "")}
        assert fs.mtime("/Users/runner/f") == 1758326400.0
        assert "stat -f %m" in run.remote

    def test_remove_does_not_mind_what_is_not_there(self, fs, run):
        run.answers = {"rm -f": (0, "", "")}
        fs.remove("/Users/runner/gone")
        assert "rm -f" in run.remote

    def test_rmtree_takes_the_tree(self, fs, run):
        fs.rmtree("/Users/runner/r/work")
        assert "rm -rf" in run.remote

    def test_clear_dir_empties_the_directory_without_replacing_it(self, fs,
                                                                   run):
        fs.clear_dir("/Users/runner/r/cache")
        assert "/Users/runner/r/cache" in run.remote
        assert "-mindepth 1" in run.remote
        assert "rmdir" not in run.remote

    def test_clear_dir_of_a_directory_that_is_not_there_is_no_error(self, fs,
                                                                    run):
        run.answers = {"find": (0, "", "")}
        fs.clear_dir("/Users/runner/r/cache")

    def test_clear_dir_that_could_not_finish_says_so(self, fs, run):
        run.answers = {"find": (1, "", "Operation not permitted")}
        with pytest.raises(OSError) as raised:
            fs.clear_dir("/Users/runner/r/cache")
        assert "Operation not permitted" in str(raised.value)

    def test_du_reports_bytes_although_the_guest_counts_kilobytes(self, fs,
                                                                  run):
        run.answers = {"du -sk": (0, "2048\t/Users/runner/r/cache\n", "")}
        assert fs.du("/Users/runner/r/cache") == 2048 * 1024

    def test_du_of_what_cannot_be_read_is_none_never_zero(self, fs, run):
        run.answers = {"du -sk": (1, "", "No such file or directory")}
        assert fs.du("/Users/runner/r/cache") is None
