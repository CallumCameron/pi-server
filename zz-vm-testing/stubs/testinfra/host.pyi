from typing import Any
from testinfra.modules.addr import Addr

class Host:
    def addr(self, addr: str) -> Addr: ...
    def check_output(self, command: str, *args: Any) -> str: ...
