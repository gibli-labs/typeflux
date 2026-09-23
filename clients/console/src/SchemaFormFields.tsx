import type { FieldPlan, InputPlan } from "./schemaForm";

/** Typed input fields rendered from a workflow's input schema (#269). */
export function SchemaForm({
  plan,
  state,
  onChange,
}: {
  plan: InputPlan;
  state: Record<string, string>;
  onChange: (key: string, value: string) => void;
}) {
  return (
    <div className="schema-form">
      {plan.fields.map((field) => (
        <label className="schema-field" key={field.key}>
          <span className="schema-field-label">
            {field.label}
            {field.required ? <span className="schema-req"> *</span> : null}
            <span className="faint"> · {field.kind}</span>
          </span>
          {renderInput(field, state[field.key] ?? "", (value) => onChange(field.key, value))}
          {field.description ? <span className="hint">{field.description}</span> : null}
        </label>
      ))}
    </div>
  );
}

function renderInput(field: FieldPlan, value: string, set: (value: string) => void) {
  if (field.kind === "boolean") {
    return (
      <select value={value} onChange={(event) => set(event.target.value)}>
        <option value="">—</option>
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    );
  }
  if (field.kind === "enum") {
    return (
      <select value={value} onChange={(event) => set(event.target.value)}>
        <option value="">—</option>
        {(field.enumValues ?? []).map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    );
  }
  if (field.kind === "complex") {
    return (
      <textarea
        className="json-input"
        value={value}
        onChange={(event) => set(event.target.value)}
        placeholder="JSON value"
        rows={3}
        spellCheck={false}
      />
    );
  }
  return (
    <input
      type={field.kind === "number" || field.kind === "integer" ? "number" : "text"}
      value={value}
      onChange={(event) => set(event.target.value)}
    />
  );
}
