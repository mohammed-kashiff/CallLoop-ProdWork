export const ccBtn =
  'inline-flex items-center justify-center rounded-lg bg-cc-btn px-4 py-2.5 text-[15px] font-semibold text-cc-on-btn disabled:cursor-not-allowed disabled:opacity-40'
export const ccGhost =
  'inline-flex items-center justify-center rounded-lg border border-cc-line bg-cc-card px-4 py-2.5 text-[15px] font-semibold text-cc-navy disabled:cursor-not-allowed disabled:opacity-40'
export const ccInput =
  'w-full rounded-lg border border-cc-line bg-cc-card px-3.5 py-2.5 text-[16px] text-cc-ink outline-none placeholder:text-cc-muted focus:border-cc-accent'
export const ccLabel = 'grid gap-1.5 text-[13px] font-semibold text-cc-muted'
export const ccTd = 'px-3.5 py-3 align-middle text-[15px] text-cc-ink'
export const ccTh =
  'px-3.5 py-3 text-left text-[13px] font-semibold text-cc-muted'
export const ccRow = 'border-b border-cc-line last:border-0 hover:bg-cc-wash'

export function adminNavClass(isActive: boolean): string {
  return [
    'rounded-lg px-2.5 py-2.5 text-[15px] font-semibold no-underline',
    isActive
      ? 'bg-cc-btn text-white hover:bg-cc-btn hover:text-white'
      : 'text-cc-rail-text hover:bg-white/10 hover:text-white',
  ].join(' ')
}
export const ccMono = 'font-mono text-[14px] text-cc-muted'
export const ccErr = 'text-[15px] font-medium text-cc-fail'
export const ccHint = 'text-[15px] leading-relaxed text-cc-muted'
