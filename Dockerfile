FROM python:3.12-slim

WORKDIR /app

COPY cursor_jiggler.py test_cursor_jiggler.py README.md Start_Cursor_Jiggler.bat jiggle.bat ./

ENTRYPOINT ["python", "cursor_jiggler.py"]
CMD ["--help"]
