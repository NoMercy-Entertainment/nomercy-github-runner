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
