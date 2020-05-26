import contextlib
import importlib
import os
import pathlib
import unittest

# Import the "obsgit" CLI as a module
file_name = pathlib.Path(pathlib.Path(__file__).parent, "..", "obsgit").resolve()
module_name = file_name.name
spec = importlib.util.spec_from_loader(
    module_name, importlib.machinery.SourceFileLoader(module_name, str(file_name))
)
obsgit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(obsgit)


class TestReadConfig(unittest.TestCase):
    config_filename = pathlib.Path("/tmp/config")

    def setUp(self):
        self._remove_config_filename()

    def tearDown(self):
        self._remove_config_filename()

    def _remove_config_filename(self):
        try:
            self.config_filename.unlink()
        except FileNotFoundError:
            pass

    def test_default_content(self):
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull):
                config = obsgit.read_config(self.config_filename)
        self.assertEqual(config["import"]["url"], "https://api.opensuse.org")
        self.assertEqual(config["import"]["username"], os.getlogin())
        self.assertEqual(config["import"]["password"], "password")
        self.assertEqual(config["export"]["url"], "https://api.opensuse.org")
        self.assertEqual(config["export"]["username"], os.getlogin())
        self.assertEqual(config["export"]["password"], "password")
        self.assertEqual(
            config["export"]["storage"], f"home:{os.getlogin()}:storage/files"
        )

    def test_default_content_when_url(self):
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull):
                config = obsgit.read_config(
                    self.config_filename, url="https://api.suse.de"
                )
        self.assertEqual(config["import"]["url"], "https://api.suse.de")
        self.assertEqual(config["import"]["username"], os.getlogin())
        self.assertEqual(config["import"]["password"], "password")
        self.assertEqual(config["export"]["url"], "https://api.suse.de")
        self.assertEqual(config["export"]["username"], os.getlogin())
        self.assertEqual(config["export"]["password"], "password")
        self.assertEqual(
            config["export"]["storage"], f"home:{os.getlogin()}:storage/files"
        )

    def test_default_content_when_username(self):
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull):
                config = obsgit.read_config(self.config_filename, username="user")
        self.assertEqual(config["import"]["url"], "https://api.opensuse.org")
        self.assertEqual(config["import"]["username"], "user")
        self.assertEqual(config["import"]["password"], "password")
        self.assertEqual(config["export"]["url"], "https://api.opensuse.org")
        self.assertEqual(config["export"]["username"], "user")
        self.assertEqual(config["export"]["password"], "password")
        self.assertEqual(config["export"]["storage"], f"home:user:storage/files")

    def test_default_content_when_password(self):
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull):
                config = obsgit.read_config(self.config_filename, password="secret")
        self.assertEqual(config["import"]["url"], "https://api.opensuse.org")
        self.assertEqual(config["import"]["username"], os.getlogin())
        self.assertEqual(config["import"]["password"], "secret")
        self.assertEqual(config["export"]["url"], "https://api.opensuse.org")
        self.assertEqual(config["export"]["username"], os.getlogin())
        self.assertEqual(config["export"]["password"], "secret")
        self.assertEqual(
            config["export"]["storage"], f"home:{os.getlogin()}:storage/files"
        )

    def test_default_persmissions(self):
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull):
                obsgit.read_config(self.config_filename)
        self.assertTrue(self.config_filename.exists())
        self.assertEqual(self.config_filename.stat().st_mode, 33152)

    def test_custom_content(self):
        with open(self.config_filename, "w") as f:
            f.write(
                """
[import]
url = https://api.import.com
username = user_import
password = passwd_import

[export]
url = https://api.export.com
username = user_export
password = passwd_export
storage = project:storage/files
"""
            )
        config = obsgit.read_config(self.config_filename)
        self.assertEqual(config["import"]["url"], "https://api.import.com")
        self.assertEqual(config["import"]["username"], "user_import")
        self.assertEqual(config["import"]["password"], "passwd_import")
        self.assertEqual(config["export"]["url"], "https://api.export.com")
        self.assertEqual(config["export"]["username"], "user_export")
        self.assertEqual(config["export"]["password"], "passwd_export")
        self.assertEqual(config["export"]["storage"], f"project:storage/files")


class TestExporterIsBinary(unittest.TestCase):
    unknown_filename = pathlib.Path("/tmp/unknown")

    def setUp(self):
        self._remove_unknown_filename()

    def tearDown(self):
        self._remove_unknown_filename()

    def _remove_unknown_filename(self):
        try:
            self.unknown_filename.unlink()
        except FileNotFoundError:
            pass

    def test_is_binary_shortcut(self):
        self.assertTrue(obsgit.Exporter.is_binary("foo.tar.gz"))

    def test_is_non_binary_shorcut(self):
        self.assertFalse(obsgit.Exporter.is_binary("foo.spec"))

    def test_is_non_binary_exception_shorcut(self):
        self.assertTrue(obsgit.Exporter.is_binary("foo.obscpio"))

    def test_is_binary(self):
        with open(self.unknown_filename, "wb") as f:
            f.write(b"MZ\xea\x07\x00\xc0\x07\x8c")
        self.assertTrue(obsgit.Exporter.is_binary(self.unknown_filename))

    def test_is_non_binary(self):
        with open(self.unknown_filename, "w") as f:
            f.write("some text")
        self.assertFalse(obsgit.Exporter.is_binary(self.unknown_filename))


if __name__ == "__main__":
    unittest.main()
