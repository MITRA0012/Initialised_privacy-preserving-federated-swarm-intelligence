export type SearchAlgorithm = 'abc' | 'pso' | 'ga';
export type SearchDirection = 'minimize' | 'maximize';
export type SearchBound = { name: string; min: number; max: number; integer?: boolean };
export type SearchCandidate = { values: Record<string, number>; score: number };
export type SearchProgress = { generation: number; best: SearchCandidate; evaluated: number };

type Bee = { x: number[]; score: number; trials: number };
const rand = (min: number, max: number) => min + Math.random() * (max - min);
const clamp = (v: number, b: SearchBound) => Math.min(b.max, Math.max(b.min, v));

export async function runSwarm(options: {
  algorithm: SearchAlgorithm; bounds: SearchBound[]; population: number; generations: number;
  direction: SearchDirection; evaluate: (values: Record<string, number>) => Promise<number>;
  progress: (state: SearchProgress) => void;
}): Promise<SearchCandidate> {
  const { algorithm, bounds, population, generations, direction, evaluate, progress } = options;
  const dims = bounds.length;
  const better = (a: number, b: number) => direction === 'minimize' ? a < b : a > b;
  const toRecord = (x: number[]) => Object.fromEntries(bounds.map((b, i) => [b.name, b.integer ? Math.round(x[i]) : Number(x[i].toPrecision(6))]));
  const score = async (x: number[]) => evaluate(toRecord(x));
  const makeCandidate = (bee: Bee): SearchCandidate => ({ values: toRecord(bee.x), score: bee.score });
  let evaluated = 0;
  const emit = (generation: number, bees: Bee[]) => {
    const best = bees.reduce((a, b) => better(b.score, a.score) ? b : a);
    progress({ generation, best: makeCandidate(best), evaluated });
  };
  let bees: Bee[] = [];
  for (let i = 0; i < population; i++) {
    const x = bounds.map(b => rand(b.min, b.max));
    bees.push({ x, score: await score(x), trials: 0 }); evaluated++;
  }
  emit(0, bees);

  if (algorithm === 'abc') {
    const limit = Math.max(5, Math.floor(population * dims / 2));
    for (let gen = 1; gen <= generations; gen++) {
      for (let i = 0; i < bees.length; i++) {
        const k = (i + 1 + Math.floor(Math.random() * (bees.length - 1))) % bees.length;
        const j = Math.floor(Math.random() * dims);
        const x = [...bees[i].x]; x[j] = clamp(x[j] + rand(-1, 1) * (x[j] - bees[k].x[j]), bounds[j]);
        const candidate = await score(x); evaluated++;
        if (better(candidate, bees[i].score)) bees[i] = { x, score: candidate, trials: 0 }; else bees[i].trials++;
      }
      const ranked = [...bees].sort((a, b) => better(a.score, b.score) ? -1 : better(b.score, a.score) ? 1 : 0);
      const weights = bees.map(b => bees.length - ranked.indexOf(b)); const total = weights.reduce((a, b) => a + b, 0);
      for (let n = 0; n < bees.length; n++) {
        let pick = Math.random() * total, i = 0;
        while (i < weights.length - 1 && (pick -= weights[i]) > 0) i++;
        const k = (i + 1 + Math.floor(Math.random() * (bees.length - 1))) % bees.length;
        const j = Math.floor(Math.random() * dims);
        const x = [...bees[i].x]; x[j] = clamp(x[j] + rand(-1, 1) * (x[j] - bees[k].x[j]), bounds[j]);
        const candidate = await score(x); evaluated++;
        if (better(candidate, bees[i].score)) bees[i] = { x, score: candidate, trials: 0 }; else bees[i].trials++;
      }
      const scout = bees.reduce((best, b, i) => b.trials > bees[best].trials ? i : best, 0);
      if (bees[scout].trials >= limit) { const x = bounds.map(b => rand(b.min, b.max)); bees[scout] = { x, score: await score(x), trials: 0 }; evaluated++; }
      emit(gen, bees);
    }
  } else if (algorithm === 'pso') {
    const velocity = bees.map(() => bounds.map(b => rand(-(b.max - b.min) * .1, (b.max - b.min) * .1)));
    const personal = bees.map(b => ({ x: [...b.x], score: b.score }));
    let global = personal.reduce((a, b) => better(b.score, a.score) ? b : a);
    for (let gen = 1; gen <= generations; gen++) {
      for (let i = 0; i < bees.length; i++) {
        for (let j = 0; j < dims; j++) {
          velocity[i][j] = .72 * velocity[i][j] + 1.45 * Math.random() * (personal[i].x[j] - bees[i].x[j]) + 1.45 * Math.random() * (global.x[j] - bees[i].x[j]);
          bees[i].x[j] = clamp(bees[i].x[j] + velocity[i][j], bounds[j]);
        }
        bees[i].score = await score(bees[i].x); evaluated++;
        if (better(bees[i].score, personal[i].score)) personal[i] = { x: [...bees[i].x], score: bees[i].score };
      }
      global = personal.reduce((a, b) => better(b.score, a.score) ? b : a); emit(gen, bees);
    }
    return { values: toRecord(global.x), score: global.score };
  } else {
    for (let gen = 1; gen <= generations; gen++) {
      const ranked = [...bees].sort((a, b) => better(a.score, b.score) ? -1 : better(b.score, a.score) ? 1 : 0);
      const next: Bee[] = ranked.slice(0, 2).map(b => ({ ...b, x: [...b.x] }));
      while (next.length < population) {
        const tournament = () => { const a = bees[Math.floor(Math.random() * bees.length)], b = bees[Math.floor(Math.random() * bees.length)]; return better(a.score, b.score) ? a : b; };
        const p = tournament(), q = tournament();
        const x = bounds.map((bound, j) => clamp((Math.random() < .5 ? p.x[j] : q.x[j]) + (Math.random() < .25 ? rand(-.12, .12) * (bound.max - bound.min) : 0), bound));
        next.push({ x, score: await score(x), trials: 0 }); evaluated++;
      }
      bees = next; emit(gen, bees);
    }
  }
  const best = bees.reduce((a, b) => better(b.score, a.score) ? b : a);
  return makeCandidate(best);
}
