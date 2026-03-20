// format.js — score badge helper (kept for backward compatibility)
// New architecture: AI returns structured HTML directly, no text-to-HTML conversion needed.

function formatZapis(text) {
  // Legacy function — new zapisy use HTML directly from AI JSON output
  // This is only called for old plain-text records
  if (!text || text.includes('<section') || text.includes('<div class="zapis-header')) {
    return text; // Already HTML, return as-is
  }
  // Basic fallback for very old records
  return '<pre style="white-space:pre-wrap;font-size:13px;line-height:1.7">' +
    text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') +
    '</pre>';
}
