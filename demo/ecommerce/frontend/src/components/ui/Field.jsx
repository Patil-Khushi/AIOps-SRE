/**
 * One field component for the whole app.
 *
 * The `icon` prop covers the icon-prefixed auth variant, which is the only
 * difference between the auth inputs and the address inputs — two nearly
 * identical field components would drift within a week.
 */
export default function Field({
  label,
  icon: Icon,
  type = "text",
  value,
  onChange,
  placeholder,
  error,
  autoComplete,
  inputMode,
  className = "",
}) {
  return (
    <div className={className}>
      {label && <label className="mb-1 block text-xs font-medium text-muted">{label}</label>}
      {Icon ? (
        <div
          className={`flex items-center gap-2 rounded-lg border bg-white px-3 transition
            focus-within:ring-2 focus-within:ring-coral/40
            ${error ? "border-coral" : "border-line focus-within:border-coral"}`}
        >
          <Icon size={15} className="shrink-0 text-muted" />
          <input
            type={type}
            value={value}
            onChange={onChange}
            placeholder={placeholder}
            autoComplete={autoComplete}
            inputMode={inputMode}
            className="w-full bg-transparent py-2 text-sm outline-none placeholder:text-muted/70"
          />
        </div>
      ) : (
        <input
          type={type}
          value={value}
          onChange={onChange}
          placeholder={placeholder}
          autoComplete={autoComplete}
          inputMode={inputMode}
          className={`field ${error ? "border-coral" : ""}`}
        />
      )}
      {error && <p className="mt-1 text-xs text-coral-dark">{error}</p>}
    </div>
  );
}
