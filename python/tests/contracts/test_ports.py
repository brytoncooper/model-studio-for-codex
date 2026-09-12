
import unittest

from model_deck_contracts import ports


class PortsTests(unittest.TestCase):
    def test_runtime_checkable_protocols_exist(self) -> None:
        for name in (
            "ApplicationPaths",
            "AtomicFileWriter",
            "CredentialStore",
            "InstanceLock",
            "LocalTransport",
            "OwnedProcessSupervisor",
            "WindowAttachment",
        ):
            self.assertTrue(hasattr(ports, name))
