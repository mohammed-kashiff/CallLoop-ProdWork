export type FeatureMap = Record<string, boolean>

export type TrialFlag = {
  key: string
  label: string
  description?: string
  /** Trial-run UI flags default on. The transcription engine defaults off. */
  defaultEnabled?: boolean
}

export function flagEnabled(features: FeatureMap | undefined, key: string): boolean {
  if (!features || features[key] === undefined) return true
  return features[key] !== false
}

export function adminFlagOn(features: FeatureMap | undefined, flag: TrialFlag): boolean {
  const value = features?.[flag.key]
  if (flag.defaultEnabled === false) return value === true
  return value !== false
}

export const TRIAL_FLAGS: TrialFlag[] = [
  { key: "show_usage_bar", label: "Usage bar" },
  { key: "show_neighbourhood_nav", label: "Neighbourhood nav" },
  { key: "show_growth_tools_nav", label: "Growth tools nav" },
  { key: "show_powered_by_pyai", label: "Powered by PyAI" },
  { key: "show_billed_usage_panel", label: "Billed usage panel" },
  {
    key: "use_selfhosted_transcription",
    label: "Self-hosted transcription",
    description:
      "Use Whisper + pyannote on this host instead of PyAI Hear. Off until you turn it on for this org.",
    defaultEnabled: false,
  },
  {
    key: "enable_bulk_call_clear",
    label: "Bulk call clear",
    description:
      "Lets this org run Clear cache, which soft-deletes every call and removes their recordings at once. Off until you turn it on for this org.",
    defaultEnabled: false,
  },
]
