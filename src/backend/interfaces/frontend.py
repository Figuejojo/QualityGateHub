"""\file frontend.py
\brief Provider for the static browser frontend asset.
"""


class FrontendProvider:
    """\class FrontendProvider
    \brief Load the frontend HTML without coupling HTTP routing to the filesystem.
    """

    def __init__(self, path):
        """\brief Configure the frontend asset location.
        \param path Path to ``index.html``.
        """
        self.path = path

    def read(self):
        """\brief Read the frontend asset.
        \return Frontend contents as UTF-8 bytes.
        """
        with open(self.path, "rb") as frontend_file:
            return frontend_file.read()
