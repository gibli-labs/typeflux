"""Child-workflow composition (#396).

Proves the durable code-defined orchestration path: a parent ``@workflow.defn`` that
composes a child ``@workflow.defn`` via ``workflow.execute_child_workflow``, the child
running a Typeflux ``AIActivity`` - the ``@workflow.defn`` counterpart to the async
:func:`typeflux.fan_out` runner. See ``README.md`` for the conventions.
"""
