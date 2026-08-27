export type FeatureMap = Record<string, boolean>

export function flagEnabled(features: FeatureMap | undefined, key: string): boolean {
  if (!features || features[key] === undefined) return true
  return features[key] !== false
}

export const TRIAL_FLAGS: { key: string; label: string }[] = [
  { key: "show_usage_bar", label: "Usage bar" },
  { key: "show_neighbourhood_nav", label: "Neighbourhood nav" },
  { key: "show_growth_tools_nav", label: "Growth tools nav" },
  { key: "show_powered_by_pyai", label: "Powered by PyAI" },
  { key: "show_billed_usage_panel", label: "Billed usage panel" },
]
