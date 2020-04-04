from typing import Any, Callable, Dict, List, Sequence, Tuple, TypeVar, cast
import inspect
import json
import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import codetiming
import pytest
import trparse
import vagrant as vagrant_lib
from _pytest.fixtures import FixtureRequest
from _pytest.mark.structures import MarkDecorator
from testinfra.host import Host
from testinfra.utils import ansible_runner


T = TypeVar('T')


class Timer(codetiming.Timer):

    def __init__(self, name: str = '[unnamed timer]') -> None:
        super(Timer, self).__init__(name=name, logger=logging.debug)
        self._args = []  # type: List[str]
        self._retval = None  # type: object
        self._extra_text = []  # type: List[object]

    @staticmethod
    def _escape_str(s: str) -> str:
        return s.replace('{', '{{').replace('}', '}}')

    def _update_text(self) -> None:
        first_line = '\u001b[1;36m{name}'
        if self._args:
            first_line += '(' + ', '.join(self._args) + ')'
        first_line += '\u001b[0m: elapsed time: {:.4f} seconds'

        msg = ''
        if self._extra_text:
            for item in self._extra_text:
                msg += Timer._escape_str(str(item)) + '\n'
        if self._retval is not None:
            msg += 'Result: ' + Timer._escape_str(str(self._retval))
        msg = '\n'.join(['    ' + line for line in msg.split('\n') if line])
        if msg:
            msg = '\n' + msg
        self.text = first_line + msg

    def set_args(self, *args: Any) -> None:
        self._args = [Timer._escape_str(str(arg)) for arg in args]

    def set_retval(self, retval: object) -> None:
        self._retval = retval

    def add_extra(self, extra: object) -> None:
        self._extra_text.append(extra)

    def __enter__(self) -> 'Timer':
        return cast(Timer, super(Timer, self).__enter__())

    def __exit__(self, *exc_info: Any) -> None:
        self._update_text()
        super(Timer, self).__exit__(*exc_info)

    def __call__(self, *args: Any) -> Any:  # pylint: disable=arguments-differ
        raise NotImplementedError("Do not use 'Timer' as a decorator, use 'timer' instead")


def timer(f: Callable[..., T]) -> Callable[..., T]:
    arg_details = OrderedDict(inspect.signature(f).parameters)
    has_self = 'self' in arg_details
    if has_self:
        del arg_details['self']
    inject = arg_details and list(arg_details.values())[0].annotation == Timer

    @wraps(f)
    def inner(*args: Any, **kwargs: Any) -> T:
        with Timer(f.__qualname__) as t:
            t.set_args(*(args[1:] if has_self else args))
            if inject and has_self:
                retval = f(*((args[0], t) + args[1:]), **kwargs)
            elif inject:
                retval = f(t, *args, **kwargs)
            else:
                retval = f(*args, **kwargs)
            t.set_retval(retval)
            return retval
    return inner


def vms_down(*args: str) -> MarkDecorator:
    return pytest.mark.vms_down(vms=args)


class Vagrant:
    @timer
    def __init__(self) -> None:
        super(Vagrant, self).__init__()
        self._v = vagrant_lib.Vagrant()
        # VM operations are slow, so we cache the state. This requires that nothing else modifies
        # the state while the tests are running.
        self._state = {vm.name: vm.state == self._v.RUNNING for vm in self._v.status()}

    def state(self) -> Dict[str, bool]:
        return self._state

    def all_vms(self) -> List[str]:
        return sorted(self._state)

    def running_vms(self) -> List[str]:
        return sorted([vm for vm, up in self._state.items() if up])

    @timer
    def up(self, vm: str) -> None:
        if not self._state[vm]:
            self._v.up(vm_name=vm)
            self._state[vm] = True

    @timer
    def down(self, vm: str) -> None:
        if self._state[vm]:
            self._v.halt(vm_name=vm)
            self._state[vm] = False

    def set_state(self, vm: str, state: bool) -> None:
        if state:
            self.up(vm)
        else:
            self.down(vm)

    def set_states(self, vms_down: Sequence[str] = ()) -> None:
        for vm in self.all_vms():
            self.set_state(vm, vm not in vms_down)


class Net:
    def __init__(self, hosts: Dict[str, Host], addrs: Dict[str, str], vagrant: Vagrant) -> None:
        super(Net, self).__init__()
        self._hosts = hosts
        self._addrs = addrs
        self._vagrant = vagrant

    @timer
    def reachable(self, host: str, addr: str) -> bool:
        return self._hosts[host].addr(self._addrs[addr]).is_reachable

    @timer
    def traceroute(self, t: Timer, host: str, addr: str) -> List[str]:
        """Gets the hops from host to addr; empty list means unreachable."""
        result = trparse.loads(
            self._hosts[host].check_output('sudo traceroute -I %s', self._addrs[addr]))
        for hop in result.hops:
            for probe in hop.probes:
                if probe.annotation:
                    return []
        out = []
        for hop in result.hops:
            ips = set()
            for probe in hop.probes:
                if probe.ip:
                    ips.add(probe.ip)
            if len(ips) == 1:
                out.append(list(ips)[0])
            elif len(ips) > 1:
                raise ValueError('Traceroute %s -> %s returned multiple IPs: %s' % (
                    host, addr, ips))
            else:
                # Traceroute returned '*'
                out.append('')

        t.add_extra(result)
        return out

    def _host_addr_pairs(self, hosts: List[str]) -> List[Tuple[str, str]]:
        running_vms = self._vagrant.running_vms()
        if sorted(hosts) != running_vms:
            raise ValueError(
                ("'hosts' must exactly match the running VMs: got %s, want %s. Either the wrong "
                 "hosts were passed in , or some VMs are in the wrong state.") % (
                     sorted(hosts), running_vms))

        out = []
        for host in sorted(hosts):
            for addr in sorted(self._addrs):
                out.append((host, addr))
        return out

    def _assert_result(
            self,
            want_fn: Callable[[str, str], T],
            got_fn: Callable[[str, str], T],
            host_addr_pairs: List[Tuple[str, str]]) -> None:
        expected = [want_fn(host, addr) for host, addr in host_addr_pairs]
        logging.debug('Running %d checks', len(host_addr_pairs))
        with ThreadPoolExecutor() as e:
            results = e.map(got_fn, *zip(*host_addr_pairs))
        incorrect = []
        for pair, want, got in zip(host_addr_pairs, expected, results):
            if want != got:
                host, addr = pair
                incorrect.append((host, addr, want, got))
        if incorrect:
            lines = ['%s gave the wrong result for the following host/addr combinations:' %
                     got_fn.__name__]
            for host, addr, want, got in incorrect:
                lines.append('  %s -> %s (%s): want %s, got %s' % (
                    host, addr, self._addrs[addr], want, got))
            pytest.fail('\n'.join(lines))

    @timer
    def assert_reachability(self, reachable: Dict[str, List[str]]) -> None:
        """Verify reachability of all host/addr pairs is as expected.

        Args:
          reachable: mapping from hostname to addr names that should be reachable
            from that host. All host/addr pairs not listed will be checked for
            being unreachable.
        """
        self._assert_result(
            lambda host, addr: addr in reachable[host],
            self.reachable,
            self._host_addr_pairs(sorted(reachable)))

    @timer
    def assert_routes(self, routes: Dict[str, Dict[str, List[str]]]) -> None:
        """Verify routes between all host/addr pairs are as expected.

        Args:
          routes: mapping from host to target addr to intermediate hops (i.e. not including the
          target itself), for all addrs that should be reachable from host. All host/addr pairs
          not listed will be checked for being unreachable.
        """
        def traceroute(host: str, addr: str) -> List[str]:
            result = self.traceroute(host, addr)  # pylint: disable=no-value-for-parameter
            if addr == 'external' and result:
                # External is a special case, in that we don't care where the packets go once they
                # leave the testbed.
                new_result = []
                for hop in result:
                    if hop not in self._addrs.values():
                        break
                    new_result.append(hop)
                result = new_result + [self._addrs[addr]]
            return result

        self._assert_result(
            lambda host, addr: [self._addrs[hop] for hop in
                                (routes[host][addr] + [addr] if addr in routes[host] else [])],
            traceroute,
            self._host_addr_pairs(sorted(routes)))


@pytest.fixture(scope='session')
def hosts() -> Dict[str, Host]:
    """Returns all hosts by name from the ansible inventory."""
    runner = ansible_runner.AnsibleRunner(
        '.vagrant/provisioners/ansible/inventory/vagrant_ansible_inventory')
    return {name: runner.get_host(name) for name in runner.get_hosts()}


@pytest.fixture(scope='session')
def addrs() -> Dict[str, str]:
    """Returns all IP addresses by name."""
    with open('config.json') as f:
        return cast(Dict[str, str], json.load(f)['addrs'])


@pytest.fixture(scope='session')
def vagrant() -> Vagrant:
    return Vagrant()


@pytest.fixture(scope='session')
def net(hosts: Dict[str, Host], addrs: Dict[str, str], vagrant: Vagrant) -> Net:
    return Net(hosts, addrs, vagrant)


@pytest.fixture(scope='function', autouse=True)
@timer
def ensure_vm_state(vagrant: Vagrant, request: FixtureRequest) -> None:
    vms_down = ()  # type: Tuple[str, ...]
    for mark in request.keywords.get('pytestmark', []):
        if mark.name == 'vms_down':
            vms_down = mark.kwargs['vms']
    vagrant.set_states(vms_down)
