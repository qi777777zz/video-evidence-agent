from video_agent import storage


class FakeS3:
    def __init__(self):
        self.created = False

    def head_bucket(self, **_kwargs):
        return None

    def create_bucket(self, **_kwargs):
        self.created = True


def test_existing_bucket_needs_no_unsupported_cors_api(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage, "client", lambda: fake)
    storage.ensure_bucket()
    assert fake.created is False
