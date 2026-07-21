/** Shared planner constants — currencies/cabins the backend accepts today. */

export type Cabin =
  | "economy"
  | "premium_economy"
  | "business"
  | "first";

export type CardProduct = "venture" | "venture_x";

export type CurrencyId =
  | "capital_one"
  | "amex_mr"
  | "chase_ur"
  | "citi_typ"
  | "bilt";

export interface CurrencyOption {
  id: CurrencyId;
  label: string;
  short: string;
  defaultMiles: number;
  /** Portal floor exists only for Capital One products today. */
  hasPortal: boolean;
}

export const CURRENCIES: CurrencyOption[] = [
  {
    id: "capital_one",
    label: "Capital One",
    short: "C1",
    defaultMiles: 90_000,
    hasPortal: true,
  },
  {
    id: "chase_ur",
    label: "Chase UR",
    short: "UR",
    defaultMiles: 100_000,
    hasPortal: true,
  },
  {
    id: "amex_mr",
    label: "Amex MR",
    short: "MR",
    defaultMiles: 100_000,
    hasPortal: true,
  },
  {
    id: "citi_typ",
    label: "Citi ThankYou",
    short: "TYP",
    defaultMiles: 80_000,
    hasPortal: true,
  },
  {
    id: "bilt",
    label: "Bilt",
    short: "Bilt",
    defaultMiles: 50_000,
    hasPortal: true,
  },
];

export const CABINS: { id: Cabin; label: string }[] = [
  { id: "economy", label: "Economy" },
  { id: "premium_economy", label: "Premium econ" },
  { id: "business", label: "Business" },
  { id: "first", label: "First" },
];

export const CARD_PRODUCTS: { id: CardProduct; label: string; cpp: string }[] = [
  { id: "venture_x", label: "Venture X", cpp: "1.25¢" },
  { id: "venture", label: "Venture", cpp: "1.0¢" },
];

/** Rough travel window — not yet wired to fare/award APIs (Amadeus uses +30d). */
export const TRAVEL_WINDOWS = [
  { id: "flexible", label: "Flexible (+/− 3 days)" },
  { id: "next_30", label: "Next 30 days" },
  { id: "next_60", label: "Next 60 days" },
  { id: "next_90", label: "Next 90 days" },
  { id: "peak_summer", label: "Peak summer (Jun–Aug)" },
  { id: "holidays", label: "Winter holidays (Dec)" },
] as const;

export type TravelWindowId = (typeof TRAVEL_WINDOWS)[number]["id"];

export function currencyLabel(id: CurrencyId): string {
  return CURRENCIES.find((c) => c.id === id)?.label ?? id;
}

export function currencyShort(id: CurrencyId): string {
  return CURRENCIES.find((c) => c.id === id)?.short ?? id;
}
