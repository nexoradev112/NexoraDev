function parseTimestamp(value: string): Date | null {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function utcParts(date: Date): { year: string; month: string; day: string; hour: string; minute: string } {
  return {
    year: String(date.getUTCFullYear()),
    month: String(date.getUTCMonth() + 1).padStart(2, "0"),
    day: String(date.getUTCDate()).padStart(2, "0"),
    hour: String(date.getUTCHours()).padStart(2, "0"),
    minute: String(date.getUTCMinutes()).padStart(2, "0"),
  };
}

export function formatDisplayDate(value: string): string {
  const date = parseTimestamp(value);
  if (!date) return value;
  const { year, month, day } = utcParts(date);
  return `${year}-${month}-${day}`;
}

export function formatDisplayDateTime(value: string): string {
  const date = parseTimestamp(value);
  if (!date) return value;
  const { year, month, day, hour, minute } = utcParts(date);
  return `${year}-${month}-${day} ${hour}:${minute} UTC`;
}
