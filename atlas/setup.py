"""Build hook that keeps closed-source Atlas modules out of the public package.

The public wheel/sdist must never contain these files; they are shipped to Pro
customers separately (see build_pro.py). Keep this list in sync with the
"CLOSED SOURCE FILES" block in the repository .gitignore - tests enforce it.
"""
from setuptools import setup
from setuptools.command.build_py import build_py

CLOSED_MODULES = {
    ("atlas", "keygen"),
    ("atlas.edge", "bridge"),
    ("atlas.query", "generate_data"),
    ("atlas.query", "train"),
    # release tooling, not part of the product
    ("atlas", "build_pro"),
    ("atlas", "check_dist"),
}


class build_py_public(build_py):
    def find_package_modules(self, package, package_dir):
        return [
            module for module in super().find_package_modules(package, package_dir)
            if (module[0], module[1]) not in CLOSED_MODULES
        ]


setup(cmdclass={"build_py": build_py_public})
