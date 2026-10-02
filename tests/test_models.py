import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from screenport.models import (
    AUTH_KEY,
    Server,
    ServerStore,
    initials,
    is_valid_screen_name,
    parse_address,
    parse_screen_list_input,
    sanitize_screen_name,
)
from screenport.settings import Preferences, SettingsStore
from screenport.sshconfig import parse_ssh_config


class ScreenNameTest(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize_screen_name("  build tools "), "build-tools")
        self.assertEqual(sanitize_screen_name("Base de données"), "Base-de-donnees")
        self.assertEqual(sanitize_screen_name("a.b;c$(id)"), "a-bcid")
        self.assertEqual(sanitize_screen_name("---"), "")
        self.assertEqual(len(sanitize_screen_name("x" * 200)), 64)

    def test_validation(self):
        self.assertTrue(is_valid_screen_name("web_1-a"))
        for bad in ("", "a b", "a.b", "é", "x" * 65):
            self.assertFalse(is_valid_screen_name(bad))

    def test_list_input(self):
        self.assertEqual(parse_screen_list_input("main, logs;build\nmain"), ["main", "logs", "build"])


class AddressTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_address("alice@example.org:2222"), ("alice", "example.org", 2222))
        self.assertEqual(parse_address("example.org"), ("", "example.org", None))
        self.assertEqual(parse_address("ssh://bob@[fe80::1]:22"), ("bob", "fe80::1", 22))
        self.assertEqual(parse_address("fe80::1"), ("", "fe80::1", None))
        self.assertEqual(parse_address("host:abc"), ("", "host:abc", None))

    def test_server_properties(self):
        server = Server(host="fe80::1", user="u", port=2200)
        self.assertEqual(server.target, "u@[fe80::1]")
        self.assertEqual(server.address, "u@[fe80::1]:2200")
        self.assertEqual(server.ssh_host, "u@fe80::1")
        self.assertEqual(Server(host="h").display_name, "h")

    def test_validate(self):
        self.assertEqual(Server(host="h", user="u").validate(), [])
        self.assertTrue(Server(host="").validate())
        self.assertTrue(Server(host="-oProxyCommand=x").validate())
        self.assertTrue(Server(host="host:abc").validate())
        self.assertTrue(Server(host="h", user="-x").validate())
        self.assertTrue(Server(host="h", port=70000).validate())
        self.assertTrue(Server(host="h", auth=AUTH_KEY).validate())

    def test_initials(self):
        self.assertEqual(initials("prod-web"), "PW")
        self.assertEqual(initials("serveur"), "SE")
        self.assertEqual(initials("192.168.1.50"), "50")


class StoreTest(unittest.TestCase):
    def test_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "servers.json"
            store = ServerStore(path)
            server = Server(name="Prod", host="h", user="u", favorite_screens=["main", "bad name"])
            store.upsert(server)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            again = ServerStore(path)
            self.assertEqual(len(again.servers), 1)
            loaded = again.get(server.id)
            self.assertEqual(loaded.favorite_screens, ["main", "bad-name"])
            self.assertIs(again.find("prod"), loaded)
            self.assertIs(again.find("u@h"), loaded)
            again.remove(server.id)
            self.assertEqual(ServerStore(path).servers, [])

    def test_corrupted_file_is_kept_aside(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "servers.json"
            path.write_text("{oops")
            self.assertEqual(ServerStore(path).servers, [])
            self.assertTrue((Path(tmp) / "servers.json.bak").exists())

    def test_settings_validation(self):
        prefs = Preferences.from_dict({"attach_mode": "weird", "scrollback": "12", "multiplex": 0})
        self.assertEqual(prefs.attach_mode, "detach")
        self.assertEqual(prefs.scrollback, 12)
        self.assertFalse(prefs.multiplex)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"window": {"sessions": [{"server": "a", "screen": "b"}, "junk"]}}))
            self.assertEqual(SettingsStore(path).window.sessions, [{"server": "a", "screen": "b"}])


class SshConfigTest(unittest.TestCase):
    def test_parse(self):
        hosts = parse_ssh_config(
            """
            # commentaire
            Host *
                ServerAliveInterval 30
            Host web web-alias
                HostName 10.0.0.5
                User deploy
                Port 2222
                IdentityFile ~/.ssh/web
            Host bastion
                HostName=bastion.example.org
                ProxyJump jump
            Match host foo
                User nobody
            """
        )
        self.assertEqual([h.alias for h in hosts], ["web", "web-alias", "bastion"])
        web = hosts[0]
        self.assertEqual((web.hostname, web.user, web.port, web.identity_file), ("10.0.0.5", "deploy", 2222, "~/.ssh/web"))
        server = web.to_server()
        self.assertEqual(server.host, "web")
        self.assertEqual(server.auth, AUTH_KEY)
        self.assertEqual(hosts[2].description, "bastion.example.org via jump")


if __name__ == "__main__":
    unittest.main()
