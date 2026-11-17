import httpx


class InjectedSaveFailure(RuntimeError):
    pass


class FaultStore:
    def __init__(self, controller):
        self.controller = controller

    def __getattr__(self, name):
        return getattr(self.controller.backing_store, name)

    async def save(self, *args, **kwargs):
        controller = self.controller
        if controller.scenario.fault == "failure_before_save" and not controller.injected:
            await controller.inject("failure_before_save")
            raise InjectedSaveFailure("Test-only failure before recording the payment result")
        return await controller.backing_store.save(*args, **kwargs)


class FaultController:
    """One fault per case, injected through test-only store/transport adapters."""

    def __init__(self, scenario, store, variant, scheduler, gateway):
        self.scenario, self.backing_store, self.variant = scenario, store, variant
        self.scheduler, self.gateway = scheduler, gateway
        self.injected, self.gateway_calls, self.effects_at_fault = False, 0, []
        self.store = FaultStore(self)
        self.transport = httpx.MockTransport(self.send)

    async def inject(self, point):
        self.effects_at_fault = await self.backing_store.charges(self.variant, self.scenario.key)
        self.injected = True
        self.scheduler.record("fault_injected", point=point,
                              observed_charges=len(self.effects_at_fault))

    async def send(self, request):
        self.gateway_calls += 1
        self.scheduler.record("gateway_attempt", attempt=self.gateway_calls)
        point = self.scenario.fault
        if point == "timeout_before_charge" and not self.injected:
            await self.inject(point)
            raise httpx.ReadTimeout("Test-only timeout before the charge", request=request)
        response = await self.gateway.handle_async_request(request)
        self.scheduler.record("gateway_responded", status_code=response.status_code)
        if point == "timeout_after_charge" and not self.injected and response.status_code == 201:
            await self.inject(point)
            await response.aclose()
            raise httpx.ReadTimeout("Test-only lost response after the committed charge", request=request)
        return response

    def evidence(self):
        return {"fault": self.scenario.fault, "fault_injected": self.injected,
                "gateway_calls": self.gateway_calls, "effects_at_fault": self.effects_at_fault}

    async def observe(self):
        payment = await self.backing_store.payment(self.variant, self.scenario.key)
        state = "absent" if payment is None else "reserved" if payment["charge_id"] is None else "completed"
        self.scheduler.record("payment_state_observed", state=state)
        return {**self.evidence(), "payment_state": state, "payment": payment}
