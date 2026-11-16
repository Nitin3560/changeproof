from ..service import create_app as build_app


def create_app():
    return build_app("vulnerable")
