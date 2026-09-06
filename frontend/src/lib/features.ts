export type FeatureMap = Record<string, boolean>

export function flagEnabled(features: FeatureMap | undefined, key: string): boolean {
  if (!features || features[key] === undefined) return true
  return features[key] !== false
}
