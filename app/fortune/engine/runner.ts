import { readFileSync } from 'node:fs';
import { Solar } from 'lunar-javascript';
import { generateChart } from './vendor/ziwei/algorithm';
import { chartDetails } from './details';
import { lunarToSolar } from './vendor/ziwei/lunar-solar';
import { calculateTrueSolarTime, resolveZiweiDayOffset, shiftGregorianDate, isValidGregorianDate, formatTrueSolarClock } from './vendor/ziwei/true-solar-time';

// stdin/stdout only: no network, environment credentials or upstream web service.
try {
  const input = JSON.parse(readFileSync(0, 'utf8'));
  const [year, month, day] = input.birth_date.split('-').map(Number);
  const [clockHour, clockMinute] = input.birth_time.split(':').map(Number);
  if (!Number.isInteger(clockHour) || clockHour < 0 || clockHour > 23 || !Number.isInteger(clockMinute) || clockMinute < 0 || clockMinute > 59) throw new Error('出生时间无效');
  let date = { year, month, day };
  if (input.calendar_type === 'lunar') {
    const converted = lunarToSolar(year, month, day, input.lunar_leap_month);
    if (!converted) throw new Error('农历日期或闰月不存在，请核对出生日期');
    date = converted;
  }
  if (!isValidGregorianDate(date.year, date.month, date.day)) throw new Error('公历日期不存在');
  const solarDate = `${date.year}-${String(date.month).padStart(2, '0')}-${String(date.day).padStart(2, '0')}`;
  // The date is compared in the selected fixed standard timezone, not server local time.
  const today = new Date(Date.now() + input.utc_offset_minutes * 60000).toISOString().slice(0, 10);
  if (solarDate > today) throw new Error('出生日期不能晚于今天');
  const solar = calculateTrueSolarTime({ ...date, clockHour, clockMinute, utcOffsetMinutes: input.utc_offset_minutes, longitude: input.longitude ?? undefined });
  const chartDate = shiftGregorianDate(date, resolveZiweiDayOffset(solar, input.late_zishi));
  const chart = generateChart({ ...chartDate, hour: solar.branch, gender: input.gender, origInput: { ...date, clockHour, clockMinute } }, { leapMonth: 'split' });
  // Display the actual birth calendar date, not the effective chart month after
  // leap-month policy / late-Zi rollover; the latter can lose the leap marker.
  const birthLunar = Solar.fromYmd(date.year, date.month, date.day).getLunar();
  const lunarDate = `${birthLunar.getYear()}年${birthLunar.getMonth() < 0 ? '闰' : ''}${Math.abs(birthLunar.getMonth())}月${birthLunar.getDay()}日`;
  process.stdout.write(JSON.stringify({ chart, details: chartDetails(chart), timing: { solar_date: solarDate, lunar_date: lunarDate, chart_date: `${chartDate.year}-${String(chartDate.month).padStart(2, '0')}-${String(chartDate.day).padStart(2, '0')}`, clock: formatTrueSolarClock(solar), corrected: solar.corrected, correction_minutes: Math.round(solar.totalCorrectionMinutes * 100) / 100, branch_index: solar.branch } }));
} catch (error) {
  process.stdout.write(JSON.stringify({ error: error instanceof Error ? error.message : '排盘失败' }));
  process.exitCode = 2;
}
