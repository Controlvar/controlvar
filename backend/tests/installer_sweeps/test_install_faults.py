"""The AWS installer beta's fault sweeps: any failing call stops install (D2), and every
guard fails closed (D2b).

A clean install against the fakes names its calls. Then, for each call in turn, a new
install whose fakes fail exactly that call: AccessDenied (D2), and for the guards and the
read-backs an empty answer and the whole response where a query was asked for (D2b). Each
must stop: a non-zero exit, no success line, and no change made after the failed call.
The few calls whose failure is harmless by design are an exact list, each of which must
still end in the clean run's changes and nothing else. One case runs the real sweep
library: a failed read in the record of the account refuses the install.

The calls a run makes beside the deploy (the watcher's reads, the heartbeat) depend on
how fast the fake deploy ends, so those are failed as a class (every one of them) rather
than one by one. The runs go in parallel, each with its own fake account.
"""

from __future__ import annotations

import pytest

from backend.tests.installer_sweeps.harness import accounts
from backend.tests.installer_sweeps.harness.install_world import (
    LOCAL_TOOLS,
    InstallWorld,
    in_parallel,
    is_write,
)

pytestmark = pytest.mark.unit

SUCCESS = f"Controlvar {accounts.RELEASE} (beta) is installed and answering at:"
WATCHER = "Stacks[0].StackStatus"
HEARTBEAT = "StackSummaries[?starts_with(StackName, 'experimentation-')].[StackName,StackStatus]"
#: Every call of these runs beside the deploy, as many times as the deploy takes: each is
#: failed as a class, with the deploy made to last (still running for the watcher's
#: failure to matter; a second long, then complete, for the heartbeat to run).
CLASSES = {
    "the-watchers-stack-reads": (
        {"service": "cloudformation", "op": "describe-stacks", "has": [WATCHER]},
        {"deploy": "block", "block_seconds": 60},
    ),
    "the-heartbeat": (
        {"service": "cloudformation", "op": "list-stacks", "has": [HEARTBEAT]},
        {"deploy_seconds": 1},
    ),
}
#: The calls whose failure is harmless by design, exactly: the heartbeat (a line a minute
#: that decides nothing); the stack scan of a region other than us-west-2 answering exactly
#: AccessDenied ("not checked", as an organisation's policy may deny it); the version of
#: the record's own CDK CLI (unreadable means "install it again", which the next step
#: does); the registry logout after the copy (the login file is removed anyway); the alarm
#: topic's subscription (the summary then says "not read"); and the first look for the
#: address, a wait for DNS and the load balancer (the next look is made, and a failure
#: there stops the install).
SCAN = "StackSummaries[?StackStatus!='DELETE_COMPLETE' && starts_with(StackName, 'experimentation-')].StackName"
HARMLESS = {
    ("class", "the-heartbeat"),
    ("cloudformation", "list-stacks", "us-east-1", SCAN, 1),
    ("cdk", "--version", None, None, 1),
    ("crane", "auth logout", None, None, 1),
    (
        "sns",
        "list-topics",
        "us-west-2",
        "Topics[?ends_with(TopicArn, ':experimentation-alerts-staging')].TopicArn",
        1,
    ),
    (
        "sns",
        "list-subscriptions-by-topic",
        "us-west-2",
        "Subscriptions[].SubscriptionArn",
        1,
    ),
    ("dig", "@ns", None, None, 1),
    ("curl", f"GET https://{accounts.INSTALL_DOMAIN}/health", None, None, 1),
}
#: ... but only for AccessDenied: an empty answer or the whole response there is not the
#: exact code, and refuses.
HARMLESS_ONLY_DENIED = {("cloudformation", "list-stacks", "us-east-1", SCAN, 1)}
#: The number of calls a clean install makes, one by one (the classes aside): a change to
#: it is a change to what install does, and this sweep must see it.
SWEPT = 101


def _query(call: dict) -> str | None:
    argv = call["argv"]
    return argv[argv.index("--query") + 1] if "--query" in argv else None


def _op(call: dict) -> str:
    """The name a fault matches for a call: the op, but for dig the server asked."""
    if call["service"] == "dig":
        return "@ns" if any(a.startswith("@ns-") for a in call["argv"]) else "@1.1.1.1"
    return call["op"] or ""


def _in_a_class(call: dict) -> str | None:
    for name, (spec, _state) in CLASSES.items():
        if (
            call["service"] == spec["service"]
            and call["op"] == spec["op"]
            and all(h in call["argv"] for h in spec["has"])
        ):
            return name
    return None


#: Whether the clean run's call answered (exit 0), by case key: NOQUERY changes nothing
#: about a call whose answer is the service's error.
ANSWERED: dict[tuple, bool] = {}


def _cases(clean_calls: list[dict]) -> list[tuple[tuple, dict]]:
    """(the case's key, the fault spec) for every call of the clean run, and one per class."""
    seen: dict[tuple, int] = {}
    cases = []
    for call in clean_calls:
        if "http://127.0.0.1:9" in call["argv"]:
            continue  # the probe: it must fail, and its own test makes it find credentials
        if _in_a_class(call):
            continue
        region = (
            call["region"]
            if call["service"] not in LOCAL_TOOLS and call["service"] != "--version"
            else None
        )
        query = _query(call)
        identity = (call["service"], _op(call), region, query)
        seen[identity] = seen.get(identity, 0) + 1
        spec = {"service": call["service"], "nth": seen[identity]}
        if call["service"] == "dig":
            spec["has"] = [a for a in call["argv"] if a.startswith("@")]
        elif call["op"]:
            spec["op"] = call["op"]
        if region:
            spec["region"] = region
        if query is not None:
            spec["has"] = [query]
        cases.append(
            ((call["service"], _op(call), region, query, seen[identity]), spec)
        )
        ANSWERED[cases[-1][0]] = call["exit"] == 0
    # A spec without "has" also matches the same operation's calls that carry a query: the
    # nth of those is counted over all of them, as the fake counts.
    fixed = []
    for key, spec in cases:
        if "has" not in spec:
            same = [
                c
                for c in clean_calls
                if c["service"] == spec["service"]
                and (c["op"] == spec.get("op", c["op"]))
                and (spec.get("region") is None or c["region"] == spec["region"])
                and "http://127.0.0.1:9" not in c["argv"]
            ]
            order = [c for c in same if _query(c) is None]
            spec = dict(spec, nth=same.index(order[key[4] - 1]) + 1)
        fixed.append((key, spec))
    return fixed + [
        (("class", name), dict(spec, state=state))
        for name, (spec, state) in CLASSES.items()
    ]


class Sweep:
    def __init__(self, root, shapes) -> None:
        self.root, self.shapes = root, shapes
        self.clean = self.world("clean")
        done = self.clean.install()
        assert done.code == 0 and self.clean.unknown == "", (
            done.err,
            self.clean.unknown,
        )
        self.clean_writes = self.clean.writes()
        self.cases = _cases(self.clean.calls)

    def world(self, name: str, stub_sweep: bool = True) -> InstallWorld:
        return InstallWorld(
            self.root / name,
            accounts.install_target(),
            self.shapes,
            stub_sweep=stub_sweep,
        )

    def run(self, keys_and_specs, mode: str) -> dict:
        """Each fault in its own install; what was wrong with each, by its key."""

        def one(i_key_spec):
            i, key, spec = i_key_spec
            world = self.world(f"{mode}-{i}")
            spec = dict(spec)
            settings = spec.pop("state", None)
            if settings:
                world.edit(lambda s: s.setdefault("cdk", {}).update(settings))
            world.fault(**spec, mode=mode)
            done = world.install()
            return key, judge(key, world, done, self.clean_writes, mode)

        found = in_parallel(
            [
                lambda x=x: one(x)
                for x in [(i, k, s) for i, (k, s) in enumerate(keys_and_specs)]
            ]
        )
        return {key: problem for key, problem in found if problem}


def judge(key, world: InstallWorld, done, clean_writes, mode: str) -> str | None:
    """None when the run ended as it must; else what was wrong."""
    if world.unknown:
        return f"unmodelled: {world.unknown}"
    faulted = [
        c for c in world.calls if c["fault"] == mode
    ]  # not a service's own error
    if not faulted:
        return "the fault never applied"
    first = faulted[0]["n"]
    later = [
        (c["service"], c["op"]) for c in world.calls if is_write(c) and c["n"] > first
    ]
    if key[0] == "class":
        # The watcher starts beside cdk deploy, so the deploy may be logged after its first
        # read: it is the change already under way, not one made after the failure.
        later = [w for w in later if w != ("cdk", "deploy")]
    if mode == "NOQUERY" and faulted[0]["exit"] != 0:
        # The call's answer is the service's error (the role not found, the stack that
        # does not exist), which no query reaches: the run is the clean run.
        if done.code != 0 or world.writes() != clean_writes:
            return f"a query over an error changed the run: exit {done.code}, {done.err[:300]}"
        return None
    if key in HARMLESS and (mode == "AccessDenied" or key not in HARMLESS_ONLY_DENIED):
        if done.code != 0 or world.writes() != clean_writes:
            return f"a harmless failure did not end as a clean run: exit {done.code}, {done.err[:300]}"
        return None
    if done.code == 0 or SUCCESS in done.out:
        return f"exit {done.code} after the failed call: that index exits 0"
    if later:
        return f"changes after the failed call: {later}"
    if not done.err.startswith(("REFUSED: ", "FAILED: ")):
        return f"no message: {done.err[:300]}"
    return None


@pytest.fixture(scope="module")
def sweep(tmp_path_factory, shapes_file) -> Sweep:
    return Sweep(tmp_path_factory.mktemp("install-faults"), shapes_file)


def test_the_sweep_sees_every_call_of_a_clean_install(sweep):
    """N pinned: a call install adds, or one it drops, changes this number."""
    one_by_one = [k for k, _ in sweep.cases if k[0] != "class"]
    assert len(one_by_one) == SWEPT, len(one_by_one)
    missing = HARMLESS - {k for k, _ in sweep.cases}
    assert missing == set(), f"harmless calls the clean run does not make: {missing}"


def test_any_failing_call_stops_the_install(sweep):
    """D2: AccessDenied at each call: non-zero exit, no success line, no change after it;
    the harmless ones end as a clean run does."""
    wrong = sweep.run(sweep.cases, "AccessDenied")
    assert wrong == {}, "\n".join(f"{k}: {v}" for k, v in wrong.items())


#: Reads the release's own deploy scripts make (run unchanged, not the beta's guards).
SCRIPT_READS = {
    ("ecs", "describe-task-definition"),
    ("ecs", "describe-tasks"),
    ("logs", "get-log-events"),
}
#: Reads install repeats until an answer says the wait is over: one empty answer is a
#: "not yet", so the guard's test fails every one of them.
POLLS = {("route53", "get-change"), ("acm", "describe-certificate")}


def _guard(key) -> bool:
    """The guards and the read-backs (D2b): every read before the first change, and every
    read of something install made or is about to change."""
    if key[0] == "class":
        return False
    service, op = key[0], key[1]
    if service == "--version" or (service, op) in SCRIPT_READS:
        return False
    if service in LOCAL_TOOLS:
        return service == "dig"
    return op.startswith(("list-", "describe-", "get-", "simulate-"))


def _guard_spec(key, spec: dict) -> dict:
    if (key[0], key[1]) in POLLS:
        return {k: v for k, v in spec.items() if k != "nth"}
    return spec


@pytest.mark.parametrize("mode", ["EMPTY", "NOQUERY"])
def test_every_guard_fails_closed(sweep, mode):
    """D2b: an empty answer, or the whole response where the call asked for one field,
    ends in a refusal or a failure, never in a change the clean run would not make."""
    reads = [
        (k, _guard_spec(k, s))
        for k, s in sweep.cases
        if _guard(k) and (mode == "EMPTY" or (k[3] is not None and ANSWERED[k]))
    ]
    assert len(reads) > 40, len(reads)
    wrong = sweep.run(reads, mode)
    assert wrong == {}, "\n".join(f"{k}: {v}" for k, v in wrong.items())


def test_a_failed_read_in_the_record_of_the_account_refuses_the_install(sweep):
    """D2's one case through the real sweep library: a read it cannot make (a sweep region
    denied) refuses the install before any change."""
    world = sweep.world("real-sweep", stub_sweep=False)
    world.fault(
        service="ssm", op="describe-parameters", region="us-east-1", mode="AccessDenied"
    )
    done = world.install()
    assert done.code == 2, done.err
    assert (
        "could not read 1 of its lists (the first: us-east-1/ssm-parameters.txt)"
        in done.err
    )
    assert world.writes() == []
    assert world.unknown == ""
