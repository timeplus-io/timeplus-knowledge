from tpk.version import get_version

# Resolved from the release tag (image build env) or `git describe`; see
# tpk/version.py. Not a hand-edited constant (#85).
__version__ = get_version()[0]
