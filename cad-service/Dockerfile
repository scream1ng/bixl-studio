FROM condaforge/miniforge3:latest

# Set the working directory
WORKDIR /app

# Install ocp using Conda from cadquery channel.
# Pin a recent ocp so the triangulation API (Poly_Triangulation.Node, etc.)
# used by the ICL module is present. Bump this version to bust the layer cache.
RUN conda install -y -c conda-forge -c cadquery "ocp>=7.7.2" python=3.11 && \
    conda clean -afy && \
    python -c "import OCP, OCP.Poly; print('OCP', OCP.__version__)"

# Copy requirements and install pip dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the backend source code
COPY . .

# Railway sets the PORT environment variable dynamically
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]