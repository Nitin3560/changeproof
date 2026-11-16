from examples.payment_service.service import create_app as build_app

VARIANT = "vulnerable"


def create_app(store, gateway_transport, hook):
    # Deliberate benchmark defect: large payments skip the idempotency claim.
    return build_app(VARIANT, store, gateway_transport, hook=hook, amount_guarded=True)
