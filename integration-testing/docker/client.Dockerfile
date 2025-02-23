FROM python:3.9
COPY ./requirements.txt .
EXPOSE 9999
VOLUME ../../:/rsconnect-python/

WORKDIR /rsconnect-python/integration-testing

RUN apt-get update && \
      apt-get -y install sudo

RUN mkdir -p /libs-client && \
    curl -fsSL https://github.com/casey/just/releases/download/1.1.2/just-1.1.2-x86_64-unknown-linux-musl.tar.gz \
    | tar -C /libs-client -xz just

ENV PATH=$PATH:/libs-client

RUN pip install rsconnect-jupyter --pre && \
    pip install pipenv && \
    jupyter-nbextension install --sys-prefix --py rsconnect_jupyter

# RUN git clone https://github.com/rstudio/rsconnect-jupyter.git && \
#     pip install twine && \
#     cd rsconnect-jupyter && \
#     make dist && \
#     pip install ./dist/rsconnect_jupyter-0.0.0-py2.py3-none-any.whl && \
#     pip install pipenv && \
#     jupyter-nbextension install --sys-prefix --py rsconnect_jupyter

CMD cd ../ && \
    rm -rf ~/.jupyter/ && \
    make deps dist && \
    pip install ./dist/rsconnect_python-*.whl && \
    jupyter-nbextension enable --sys-prefix --py rsconnect_jupyter && \
    jupyter-serverextension enable --sys-prefix --py rsconnect_jupyter && \
    jupyter-notebook \
        -y --ip='0.0.0.0' --port=9999 --no-browser --NotebookApp.token='' --allow-root