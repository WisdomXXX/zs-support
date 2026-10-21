"""Also supports Ubuntu 22.04's setuptools without PEP 621 support."""
from pathlib import Path
from setuptools import setup, find_packages

root = Path(__file__).parent
(root / "src/zs_support/resources/VERSION").write_text((root / "VERSION").read_text())
setup(name="zs-support", version=(root / "VERSION").read_text().strip(),
      description="ZhiShen robot support data collector", python_requires=">=3.10",
      package_dir={"": "src"}, packages=find_packages("src"),
      package_data={"zs_support": ["resources/products/*.json", "resources/topics/*.json",
                                    "resources/completions/*", "resources/VERSION"]},
      install_requires=["PyYAML>=5.4"],
      entry_points={"console_scripts": ["zs-support=zs_support.cli:main"]})
