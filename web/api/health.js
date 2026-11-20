// Tells the page whether Genie smart mode (Claude) is switched on. Quick mode always works.
module.exports = (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  res.status(200).json({ ok: true, ai: Boolean(process.env.ANTHROPIC_API_KEY) });
};
