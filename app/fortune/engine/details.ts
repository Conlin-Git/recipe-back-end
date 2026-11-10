import type { ZiweiChart, SiHua } from './vendor/ziwei/types';
import { BRANCHES, STEMS } from './vendor/ziwei/constants';
import { getDaXianSiHua, getLiuNianSiHua, getYearBranchIndex, detectSelfSihua, findIncomingPalaces } from './vendor/ziwei/sihua';
import { CHILDHOOD_DAXIAN_INDEX, getChildhoodDaXianPalace, getDaXianIndexForAge } from './vendor/ziwei/yunxian-context';
import { detectPatterns, getMingGongSummary } from './vendor/ziwei/patterns';
import { jiangQianShenName, suiQianShenName, boshiShenName } from './vendor/ziwei/shier-shen';

// Adapt upstream computations for Vue without recreating the astrology rules.
export function chartDetails(chart: ZiweiChart) {
  const marks = (transforms: Record<SiHua, string> | undefined) =>
    Object.entries(transforms ?? {}).map(([type, star]) => ({
      type, star, palace: chart.palaces.find(p => p.stars.some(s => s.name === star))?.name ?? '',
    }));
  const periods = chart.daXians.map((dx, index) => ({
    index, start_age: dx.startAge, end_age: dx.endAge,
    start_year: chart.lunarInfo.lunarYear + dx.startAge - 1,
    end_year: chart.lunarInfo.lunarYear + dx.endAge - 1,
    palace_branch: dx.palaceBranch, palace_name: dx.palaceName,
    sihua: marks(getDaXianSiHua(chart, index)?.transforms),
  }));
  const lastAge = chart.daXians.at(-1)?.endAge ?? 80;
  const years = Array.from({ length: lastAge }, (_, i) => {
    const age = i + 1;
    const year = chart.lunarInfo.lunarYear + i;
    const periodIndex = getDaXianIndexForAge(chart, age);
    const dxIndex = periodIndex < 0 ? CHILDHOOD_DAXIAN_INDEX : periodIndex;
    const palaceBranch = getYearBranchIndex(year);
    const annual = getLiuNianSiHua(year);
    const periodBranch = periodIndex < 0
      ? getChildhoodDaXianPalace(chart, year)?.branch
      : chart.daXians[periodIndex].palaceBranch;
    return {
      age, year, ganzhi: annual.stemName + BRANCHES[palaceBranch],
      palace_branch: palaceBranch,
      palace_name: chart.palaces.find(p => p.branch === palaceBranch)?.name ?? '',
      period_index: periodIndex, period_branch: periodBranch,
      sihua: marks(annual.transforms),
      period_sihua: marks(getDaXianSiHua(chart, dxIndex, year)?.transforms),
    };
  });
  const palaceDetails = chart.palaces.map(p => ({
    branch: p.branch, stem_branch: STEMS[p.stem] + BRANCHES[p.branch],
    self_sihua: detectSelfSihua(p),
    incoming: p.stars.filter(s => s.siHua).map(s => ({
      star: s.name, type: s.siHua,
      from: findIncomingPalaces(chart, s.name, s.siHua!).map(source => source.name),
    })),
    spirits: [
      jiangQianShenName(chart.lunarInfo.yearBranch, p.branch),
      suiQianShenName(chart.lunarInfo.yearBranch, p.branch),
      boshiShenName(chart.lunarInfo.yearStem, chart.birthInfo.gender, p.branch),
    ],
  }));
  return { periods, years, palace_details: palaceDetails, patterns: detectPatterns(chart), ming_summary: getMingGongSummary(chart) };
}
