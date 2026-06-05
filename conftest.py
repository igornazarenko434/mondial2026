import sys, os
sys.path.insert(0, os.path.dirname(__file__))
# Tests don't need OTel spans — silence the console exporter so test runs stay clean.
os.environ.setdefault("OTEL_TRACES_EXPORTER", "none")
