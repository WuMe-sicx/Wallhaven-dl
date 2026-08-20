FROM python:3

RUN mkdir -p /Wallhaven-dl/Wallhaven
WORKDIR /Wallhaven-dl

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

VOLUME /Wallhaven-dl/Wallhaven

ENV WALLHAVEN_API_KEY=""

CMD [ "python", "./wallhaven-dl.py" ]
