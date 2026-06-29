import { useEffect, useId, useRef, useState } from "react";
import { Airport, resolveAirport, searchAirports } from "./airports";

interface AirportInputProps {
  id: string;
  label: string;
  value: string;
  placeholder: string;
  onChange: (code: string) => void;
  excludeCode?: string;
}

export default function AirportInput({
  id,
  label,
  value,
  placeholder,
  onChange,
  excludeCode,
}: AirportInputProps) {
  const listId = useId();
  const wrapRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState(value);
  const [highlight, setHighlight] = useState(0);

  useEffect(() => {
    setQuery(value);
  }, [value]);

  const suggestions = searchAirports(query).filter(
    (a) => a.code !== excludeCode,
  );

  function pick(airport: Airport) {
    onChange(airport.code);
    setQuery(airport.code);
    setOpen(false);
  }

  function handleChange(raw: string) {
    const next = raw.toUpperCase().replace(/[^A-Z]/g, "").slice(0, 3);
    setQuery(next);
    setOpen(true);
    setHighlight(0);
    if (next.length === 3) {
      const resolved = resolveAirport(next);
      if (resolved) onChange(resolved.code);
      else onChange(next);
    } else {
      onChange(next);
    }
  }

  function handleBlur() {
    window.setTimeout(() => {
      setOpen(false);
      const resolved = resolveAirport(query);
      if (resolved) pick(resolved);
    }, 120);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!open || suggestions.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => Math.min(h + 1, suggestions.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter" && suggestions[highlight]) {
      e.preventDefault();
      pick(suggestions[highlight]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  const resolved = resolveAirport(query);
  const invalid = query.length > 0 && !resolved;

  return (
    <div className="field airport-field" ref={wrapRef}>
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type="text"
        role="combobox"
        aria-expanded={open && suggestions.length > 0}
        aria-controls={listId}
        aria-autocomplete="list"
        placeholder={placeholder}
        value={query}
        autoComplete="off"
        spellCheck={false}
        className={invalid ? "invalid" : undefined}
        onChange={(e) => handleChange(e.target.value)}
        onFocus={() => setOpen(true)}
        onBlur={handleBlur}
        onKeyDown={handleKeyDown}
        required
      />
      {resolved && <span className="airport-hint">{resolved.city}</span>}
      {open && suggestions.length > 0 && (
        <ul id={listId} className="airport-suggestions" role="listbox">
          {suggestions.map((airport, i) => (
            <li
              key={airport.code}
              role="option"
              aria-selected={i === highlight}
              className={i === highlight ? "active" : undefined}
              onMouseDown={(e) => {
                e.preventDefault();
                pick(airport);
              }}
            >
              <strong>{airport.code}</strong>
              <span>
                {airport.city} · {airport.name}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
