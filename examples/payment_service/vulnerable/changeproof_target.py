from examples.payment_service.service import create_app as build_app

VARIANT = "vulnerable"


def create_app(store, gateway_transport, hook):
    return build_app(VARIANT, store, gateway_transport, hook=hook)
