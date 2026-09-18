"""Every module the app needs must actually be in the image.

The Dockerfile copies modules by name. Adding a module and forgetting the COPY
passes every test here and then crashes the container on import - which is
discovered in production, on a dashboard nobody can reach to find out why.
"""
import glob
import os

DASH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_image_copies_every_module_the_app_imports():
    with open(os.path.join(DASH, "Dockerfile"), encoding="utf-8") as fh:
        dockerfile = fh.read()

    modules = sorted(os.path.basename(p)
                     for p in glob.glob(os.path.join(DASH, "*.py")))
    missing = [m for m in modules if m not in dockerfile]
    assert missing == [], f"not COPYed into the image: {missing}"


def test_the_image_copies_every_package_too():
    """The check above only sees top-level modules.

    `runtime/` and `store/` are packages, and a package left out of the
    Dockerfile fails exactly the same way as a module: the container starts,
    imports, and dies - on a dashboard nobody can reach to find out why. This
    was a real gap until `store/` was added and nothing would have caught it.
    """
    with open(os.path.join(DASH, "Dockerfile"), encoding="utf-8") as fh:
        dockerfile = fh.read()

    packages = sorted(
        os.path.basename(os.path.dirname(p))
        for p in glob.glob(os.path.join(DASH, "*", "__init__.py"))
        if os.path.basename(os.path.dirname(p)) != "tests")

    assert packages, "sanity: there should be packages to check"
    missing = [p for p in packages if f"COPY {p}/" not in dockerfile]
    assert missing == [], f"not COPYed into the image: {missing}"
