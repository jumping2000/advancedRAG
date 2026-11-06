// SPEC-2026-11-06-fase-1-fondazioni
import englishTexts from "./en.json";
import italianTexts from "./it.json";

type Tree = { [key: string]: string | Tree };

function entries(tree: Tree, prefix = ""): [string, unknown][] {
  return Object.entries(tree).flatMap(([key, value]) =>
    typeof value === "object" && value !== null
      ? entries(value, `${prefix}${key}.`)
      : [[`${prefix}${key}`, value] as [string, unknown]],
  );
}

const italian = entries(italianTexts as Tree);
const english = entries(englishTexts as Tree);

it("SPEC-2026-11-06-fase-1-fondazioni/AC-029: Italian and English translations have the same keys", () => {
  const italianKeys = italian.map(([key]) => key).sort();
  const englishKeys = english.map(([key]) => key).sort();
  expect(italianKeys.length).toBeGreaterThan(0);
  expect(italianKeys).toEqual(englishKeys);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-029: every text exists in both languages (no empty values)", () => {
  for (const [key, value] of [...italian, ...english]) {
    expect(typeof value, key).toBe("string");
    expect((value as string).trim(), key).not.toBe("");
  }
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-029: every API error code of the login page is translated", () => {
  for (const code of ["invalid_credentials", "not_authenticated", "validation_error", "unknown"]) {
    expect(italian.map(([key]) => key)).toContain(`errors.${code}`);
  }
});
