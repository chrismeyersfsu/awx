from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def init_tracing(service_name: str):
    resource = Resource.create(attributes={SERVICE_NAME: service_name})

    trace.set_tracer_provider(TracerProvider(resource=resource))
    span_processor = BatchSpanProcessor(OTLPSpanExporter(endpoint="http://tempo:4317", insecure=True))
    trace.get_tracer_provider().add_span_processor(span_processor)

    # tracer = trace.get_tracer(AWX_TRACER_API)
    # span = trace.get_current_span(context)


def get_span_id(span):
    context = span.get_span_context()
    context_hex = trace.format_trace_id(context.trace_id)
    return context_hex
