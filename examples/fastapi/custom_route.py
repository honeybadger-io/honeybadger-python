from fastapi import FastAPI, HTTPException, APIRouter
from honeybadger import honeybadger, contrib
import pydantic

honeybadger.configure(api_key='<your-api-key>')

app = FastAPI(title="Honeybadger - FastAPI with Custom Route.")
app.router.route_class = contrib.HoneybadgerRoute


@app.get("/raise_some_error", tags=["Notify"])
def raise_some_error(a: str = "foo"):
    """Raises an error."""
    raise Exception(f"SomeError Occurred (a = {a})")


class DivideRequest(pydantic.BaseModel):
    a: int
    b: int = 0


@app.post("/divide", response_model=float, tags=["Notify"])
def divide(req: DivideRequest):
    """Divides `a` by `b`."""
    return req.a / req.b


@app.post("/raise_status_code", tags=["Don't Notify"])
def raise_status_code(status_code: int = 404, detail: str = "Forced 404."):
    """This exception is raised on purpose, so will not be notified."""
    raise HTTPException(status_code=404, detail=detail)


some_router = APIRouter(route_class=contrib.HoneybadgerRoute)


@some_router.get("/some_router/endpoint", tags=["Notify"])
def some_router_endpoint():
    """Try raising an error from a router."""
    raise Exception("Exception Raised by some router endpoint.")


app.include_router(some_router)
