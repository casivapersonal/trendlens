// Public VAPID key for web push (set VAPID_PUBLIC_KEY in Vercel > Settings > Environment Variables).
module.exports = (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  res.status(200).json({ key: process.env.VAPID_PUBLIC_KEY || null });
};
