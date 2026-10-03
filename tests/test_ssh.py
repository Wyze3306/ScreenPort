import os
import shutil
import subprocess
import unittest

from screenport import ssh
from screenport.models import AUTH_AGENT, AUTH_KEY, AUTH_PASSWORD, Server

SCREEN_LS = """There are screens on:
\t12345.main\t(10/02/26 15:19:44)\t(Detached)
\t678.logs\t(10/02/2026 03:19:44 PM)\t(Attached)
\t91.shared\t(Multi, attached)
\t5.old\t(Dead ???)
3 Sockets in /run/screen/S-demo.
"""


class ScreenLsParsingTest(unittest.TestCase):
    def test_sessions(self):
        sessions = ssh.parse_screen_ls(SCREEN_LS)
        self.assertEqual([s.name for s in sessions], ["main", "logs", "shared", "old"])
        main, logs, shared, old = sessions
        self.assertEqual(main.pid, 12345)
        self.assertEqual(main.id, "12345.main")
        self.assertFalse(main.attached)
        self.assertEqual(main.state_label, "Détaché")
        self.assertEqual(main.pretty_date, "02/10/2026 à 15:19")
        self.assertTrue(logs.attached)
        self.assertEqual(logs.pretty_date, "02/10/2026 à 15:19")
        self.assertTrue(shared.attached)
        self.assertEqual(shared.state_label, "Attaché (partagé)")
        self.assertTrue(old.dead)

    def test_no_sessions(self):
        self.assertEqual(ssh.parse_screen_ls("No Sockets found in /run/screen/S-demo.\n"), [])

    def test_space_separated(self):
        sessions = ssh.parse_screen_ls("  42.build (Detached)\n")
        self.assertEqual(sessions[0].name, "build")
        self.assertEqual(sessions[0].state, "Detached")

    def test_list_output_markers(self):
        out = "motd noise\n__SCREENPORT_BEGIN__\n__SCREENPORT_SCREEN__=yes\n" + SCREEN_LS + "__SCREENPORT_END__\n"
        listing = ssh.parse_list_output(out)
        self.assertTrue(listing.ok)
        self.assertTrue(listing.has_screen)
        self.assertEqual(len(listing.sessions), 4)

    def test_list_output_without_screen(self):
        listing = ssh.parse_list_output("__SCREENPORT_BEGIN__\n__SCREENPORT_SCREEN__=no\n__SCREENPORT_END__\n")
        self.assertTrue(listing.ok)
        self.assertFalse(listing.has_screen)

    def test_list_output_garbage(self):
        self.assertFalse(ssh.parse_list_output("Permission denied").ok)


class RemoteCommandTest(unittest.TestCase):
    def test_attach_command_is_safe_single_line(self):
        for command in (
            ssh.remote_attach_command("main"),
            ssh.remote_attach_command("web-1", ssh.ATTACH_SHARE, 5000),
            ssh.remote_list_command(),
            ssh.remote_kill_command("main"),
            ssh.remote_rename_command("a", "b"),
            ssh.remote_shell_command(),
        ):
            self.assertTrue(command.startswith("sh -c '"))
            self.assertNotIn("\n", command)
            self.assertNotIn("!", command)
            # Une seule paire d'apostrophes : le script n'en contient aucune.
            self.assertEqual(command.count("'"), 2)

    def test_invalid_names_rejected(self):
        for bad in ("", "a b", "x;rm -rf /", "$(id)", "a'b", "é", "main\n"):
            with self.assertRaises(ValueError):
                ssh.remote_attach_command(bad)
            with self.assertRaises(ValueError):
                ssh.remote_kill_command(bad)

    def test_history_is_clamped(self):
        self.assertIn(" 100", ssh.remote_attach_command("main", history=1))

    def test_shell_only(self):
        self.assertIn("exec", ssh.remote_attach_command(ssh.SHELL_ONLY))

    def test_ready_marker(self):
        self.assertIn(ssh.READY_TITLE, ssh.remote_attach_command("main"))
        self.assertIn(ssh.READY_TITLE, ssh.remote_shell_command())

    @unittest.skipUnless(shutil.which("sh"), "sh requis")
    def test_scripts_parse_in_sh(self):
        """Chaque script doit être syntaxiquement valide pour sh -n."""
        for command in (
            ssh.remote_attach_command("main"),
            ssh.remote_list_command(),
            ssh.remote_kill_command("main"),
            ssh.remote_rename_command("a", "b"),
        ):
            script = command.split("'")[1]
            result = subprocess.run(["sh", "-n", "-c", script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class SshArgvTest(unittest.TestCase):
    def setUp(self):
        self.options = ssh.SshOptions()

    def test_password_server(self):
        server = Server(host="example.org", user="alice", port=2200, auth=AUTH_PASSWORD)
        argv = ssh.build_ssh_argv(server, self.options, remote_command="true", runtime_dir="/run/x")
        self.assertEqual(argv[:2], ["ssh", "-t"])
        self.assertIn("2200", argv)
        self.assertIn("PreferredAuthentications=password,keyboard-interactive,publickey", argv)
        self.assertIn("ControlMaster=auto", argv)
        self.assertIn("ControlPath=/run/x/cm-%C", argv)
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertIn("IPQoS=none", argv)
        separator = argv.index("--")
        self.assertEqual(argv[separator + 1:], ["alice@example.org", "true"])

    def test_key_server_no_tty_no_mux(self):
        server = Server(host="h", user="bob", auth=AUTH_KEY, key_path="~/.ssh/id_ed25519")
        argv = ssh.build_ssh_argv(server, ssh.SshOptions(multiplex=False), tty=False, password_prompts=1)
        self.assertIn("-T", argv)
        self.assertIn("-n", argv)
        self.assertIn(os.path.expanduser("~/.ssh/id_ed25519"), argv)
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertIn("NumberOfPasswordPrompts=1", argv)
        self.assertFalse(any(a.startswith("ControlMaster") for a in argv))

    def test_agent_server_without_user(self):
        server = Server(host="h", auth=AUTH_AGENT)
        argv = ssh.build_ssh_argv(server, ssh.SshOptions(accept_new_hostkeys=False, keepalive=0))
        self.assertEqual(argv[-1], "h")
        # Port par défaut : pas de -p, ~/.ssh/config garde la main.
        self.assertNotIn("-p", argv)
        self.assertFalse(any(a.startswith("StrictHostKeyChecking") for a in argv))
        self.assertFalse(any(a.startswith("ServerAlive") for a in argv))

    def test_qos_marking_can_be_kept(self):
        server = Server(host="h", auth=AUTH_AGENT, disable_qos=False)
        argv = ssh.build_ssh_argv(server, ssh.SshOptions())
        # Option désactivée : ~/.ssh/config garde la main sur IPQoS.
        self.assertFalse(any(a.startswith("IPQoS") for a in argv))

    def test_user_strict_host_key_checking_is_respected(self):
        server = Server(host="h", auth=AUTH_AGENT)
        strict = ssh.ResolvedConfig(user="u", hosts=("h",), strict_host_key_checking="true")
        argv = ssh.build_ssh_argv(server, ssh.SshOptions(), resolved=strict)
        self.assertFalse(any(a.startswith("StrictHostKeyChecking") for a in argv))

    def test_askpass_env_never_contains_the_secret(self):
        server = Server(host="h", user="u", auth=AUTH_PASSWORD)
        resolved = ssh.ResolvedConfig(user="u", hosts=("10.0.0.1", "alias"))
        env = ssh.askpass_env("/bin/askpass", "/run/secret-x", server, resolved)
        self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "force")
        self.assertEqual(env["SCREENPORT_SECRET_FILE"], "/run/secret-x")
        self.assertEqual(env["SCREENPORT_EXPECT_HOSTS"], "10.0.0.1 alias")
        self.assertNotIn("s3cret", "".join(env.values()))
        self.assertEqual(ssh.askpass_env("/bin/askpass", None, server, force=False), {})
        self.assertNotIn("SCREENPORT_SECRET_FILE", ssh.askpass_env("/bin/askpass", None, server))

    def test_parse_ssh_g(self):
        values = ssh.parse_ssh_g("user demo\nhostname 10.0.0.1\nport 22\nstricthostkeychecking ask\n")
        self.assertEqual(values["hostname"], "10.0.0.1")
        self.assertEqual(values["user"], "demo")


class SecretPromptTest(unittest.TestCase):
    hosts = ["10.0.0.1"]

    def match(self, prompt, kind="password", key=""):
        return ssh.secret_prompt_matches(prompt, kind, "demo", self.hosts, key)

    def test_password_prompts_of_ssh(self):
        self.assertTrue(self.match("demo@10.0.0.1's password: "))
        self.assertTrue(self.match("(demo@10.0.0.1) Password: "))
        self.assertTrue(self.match("(demo@10.0.0.1) Mot de passe : "))

    def test_foreign_prompts_are_refused(self):
        # Hôte de rebond (ProxyJump), autre utilisateur, question du serveur.
        self.assertFalse(self.match("ops@jump's password: "))
        self.assertFalse(self.match("root@10.0.0.1's password: "))
        self.assertFalse(self.match("(demo@10.0.0.1) Verification code: "))
        self.assertFalse(self.match("(demo@10.0.0.1) Enter passphrase for key '/k': ", "passphrase", "/k"))
        self.assertFalse(self.match("(demo@10.0.0.1) Enter your password please: "))

    def test_passphrase_only_for_expected_key(self):
        self.assertTrue(self.match("Enter passphrase for key '/home/u/.ssh/id': ", "passphrase", "/home/u/.ssh/id"))
        self.assertFalse(self.match("Enter passphrase for key '/home/u/.ssh/other': ", "passphrase", "/home/u/.ssh/id"))
        self.assertFalse(self.match("Enter passphrase for key '/home/u/.ssh/id': ", "password", "/home/u/.ssh/id"))

    def test_command_preview(self):
        server = Server(host="h", user="u", port=2222, auth=AUTH_KEY, key_path="~/k")
        self.assertEqual(ssh.command_preview(server), "ssh -o IPQoS=none -p 2222 -i '~/k' u@h")
        server.disable_qos = False
        self.assertEqual(ssh.command_preview(server), "ssh -p 2222 -i '~/k' u@h")


class ErrorTest(unittest.TestCase):
    def test_known_errors(self):
        cases = {
            "demo@h: Permission denied (publickey,password).": "Authentification refusée",
            "ssh: Could not resolve hostname nope: Name or service not known": "Serveur introuvable",
            "ssh: connect to host h port 22: Connection refused": "Connexion refusée",
            "ssh: connect to host h port 22: Connection timed out": "Délai dépassé",
            "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@": "La clé du serveur a changé",
        }
        for stderr, title in cases.items():
            self.assertEqual(ssh.humanize_ssh_error(stderr, 255).title, title)
        self.assertTrue(ssh.humanize_ssh_error("Permission denied", 255).auth_failed)

    def test_unknown_error_keeps_last_line(self):
        error = ssh.humanize_ssh_error("first\nsomething odd happened", 255)
        self.assertEqual(error.detail, "something odd happened")


if __name__ == "__main__":
    unittest.main()
