from importlib.resources import files


def version():
    return files("zs_support").joinpath("resources/VERSION").read_text().strip()
