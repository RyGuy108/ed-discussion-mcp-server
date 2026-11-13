import httpx

from ed_api import EdAPI
from ed_service import EdService
from server import make_server
from tests.fixtures import FakeEd

if __name__ == "__main__":
    make_server(EdService(EdAPI("fixture-token", transport=httpx.MockTransport(FakeEd())))).run()
