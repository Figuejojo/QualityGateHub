"""Static frontend asset provider."""


class FrontendProvider:
    def __init__(self, path):
        self.path = path

    def read(self):
        with open(self.path, "rb") as frontend_file:
            return frontend_file.read()
