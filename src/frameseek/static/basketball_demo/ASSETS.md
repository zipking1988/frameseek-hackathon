# Synthetic basketball demo assets

The court, pass and shot source illustrations are the existing AI-generated
landing assets documented in `docs/sports-landing-20261029/assets.md`. They do
not depict a real game or private uploaded footage.

`scripts/build_basketball_demo_media.py` encodes the three stills as an 18-second
silent H.264 sequence, then extracts the 00:00, 00:06 and 00:13 JPEG evidence
frames from that file. Scene changes are edits, not continuous player motion.
The English answers are deterministic presets and do not call a model.
