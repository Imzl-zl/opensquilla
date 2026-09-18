/** Stable references to skill instructions selected for one submitted message. */
export interface SelectedSkillRef {
  name: string
  instanceId: string
  digest: string
}

export function isSelectedSkills(value: unknown): value is SelectedSkillRef[] {
  return Array.isArray(value) && value.length <= 16 && value.every(item => (
    item !== null && typeof item === 'object'
    && ['name', 'instanceId', 'digest'].every(key => (
      typeof item[key] === 'string' && item[key].trim().length > 0
    ))
  ))
}

export function copySelectedSkills(value: readonly SelectedSkillRef[] = []): SelectedSkillRef[] {
  return value.map(({ name, instanceId, digest }) => ({ name, instanceId, digest }))
}

export function sameSelectedSkills(
  left: readonly SelectedSkillRef[] = [], right: readonly SelectedSkillRef[] = [],
): boolean {
  return left.length === right.length && left.every((skill, index) => (
    skill.name === right[index]?.name
    && skill.instanceId === right[index]?.instanceId
    && skill.digest === right[index]?.digest
  ))
}
