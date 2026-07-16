import { useCallback, useEffect, useState } from "react";

import { fetchSkills } from "@/lib/api";
import type { SkillSummary } from "@/lib/types";

export function useSkills(token: string): SkillSummary[] {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const refresh = useCallback(() => {
    fetchSkills(token).then(({ skills: nextSkills }) => setSkills(nextSkills)).catch(() => setSkills([]));
  }, [token]);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetchSkills(token)
        .then(({ skills: nextSkills }) => !cancelled && setSkills(nextSkills))
        .catch(() => !cancelled && setSkills([]));
    };
    load();
    window.addEventListener("nanobot.skills.changed", load);
    return () => {
      cancelled = true;
      window.removeEventListener("nanobot.skills.changed", load);
    };
  }, [refresh, token]);

  return skills;
}
